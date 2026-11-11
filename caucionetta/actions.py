"""What the CLI and the MCP server can do, in one place: read-only views, and writes behind propose → apply.

A write first returns a preview and a short-lived, single-use confirmation id; only `apply(id)` executes it, and
only when live mode is allowed for this process (`live = true` in the config AND the process was started with
--live / --allow-live) and the bot isn't paused. Nothing here bypasses the closed endpoint list in api.py.
"""
from __future__ import annotations

import datetime as dt
import secrets
import time

from . import api as iolapi
from . import bot, config, status


class ActionError(Exception):
    def __init__(self, msg: str, code: str = "error"):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- reads

def read_status(cfg: dict, api=None, refresh: bool = False, live_flag: bool = False) -> dict:
    if refresh:
        c = cfg["bot"]
        t = config.now_local(cfg)
        con = bot.db()
        try:
            market = "abierto" if t.weekday() < 5 and bot.within(t, c["market"]) else "cerrado"
            status.refresh(api or iolapi.make_client(cfg), con, c, t, market, force=True,
                           dry=not bot.is_live(cfg, live_flag))
        finally:
            con.close()
    data = status.read()
    if data is None:
        raise ActionError("todavía no hay status.json: corré `caucionetta status --refresh` o dejá correr el bot", "no_status")
    return data


def cauciones(cfg: dict, api=None) -> dict:
    """Peso caución rates now, per business-day term, with their baseline (calls IOL: terms + one book per term)."""
    c = cfg["bot"]
    api = api or iolapi.make_client(cfg)
    t = config.now_local(cfg)
    con = bot.db()
    try:
        terms = bot.all_terms(api, c, t)
        flags = bot.day_flags(terms, t)
        rows = bot.snapshot(api, con, c, t, terms=terms, flags=flags)
        out = []
        for r in rows:
            h = bot.history(con, r["bdays"], t, c)
            out.append({"bdays": r["bdays"], "cal_days": r["cal_days"], "due_date": str(r["due_date"])[:10],
                        "taker_pays": r["take_rate"], "lender_asks": r["offer_rate"],
                        "baseline": round(h["median"], 2) if h["median"] else None, "baseline_source": h["source"]})
    finally:
        con.close()
    kinds = [k for k in ("long_closure", "month_end", "season", "friday") if flags.get(k)]
    return {"at": t.isoformat(timespec="seconds"), "currency": c["currency"], "high_probability_day": flags["high"],
            "day_kind": kinds, "final_at": bot.final_time(c, flags), "terms": out}


def positions_summary(cfg: dict, api=None) -> dict:
    api = api or iolapi.make_client(cfg)
    b = api.balances() or {}
    ars = b.get("ars") or {}
    pos = []
    for p in api.positions() or []:
        sym = ((p.get("instrument") or {}).get("symbol")) or p.get("symbol")
        av = p.get("availability") or {}
        mv = p.get("market_value") or {}
        qty = status._f(av.get("quantity_available")) + status._f(av.get("quantity_pending"))
        pos.append({"symbol": sym, "quantity": qty, "price": status._f(mv.get("current_price")),
                    "value": round(qty * status._f(mv.get("current_price")), 2)})
    return {"ars": {"available_t0": status._f((ars.get("available") or {}).get("t0")),
                    "committed_t0": status._f((ars.get("committed") or {}).get("t0"))},
            "positions": pos}


def bot_log(days: int = 3, cfg: dict | None = None) -> list[dict]:
    since = (config.now_local(cfg).date() - dt.timedelta(days=max(0, int(days)))).isoformat()
    con = bot.db()
    try:
        return [dict(r) for r in con.execute(
            "select ts, kind, bdays, term_days, amount, rate, baseline, net_tna, status, dry, reason, error, order_id "
            "from decisions where day>=? order by ts", (since,))]
    finally:
        con.close()


def config_summary(cfg: dict, live_flag: bool) -> dict:
    ok, why = config.live_allowed(cfg, live_flag)
    b = cfg["bot"]
    return {"config_file": cfg.get("_path"), "state_dir": str(config.state_dir()), "live_in_config": cfg["live"],
            "live_allowed_now": ok and not bot.paused(), "why": "pausado" if ok and bot.paused() else why,
            "paused": bot.paused(),
            "limits": {k: b[k] for k in ("max_orders_day", "max_errors_day", "max_order_amount", "keep_ars",
                                         "min_amount", "min_net_tna")},
            "terms_days": b["terms_days"], "money_market": {"enabled": b["mm"]["enabled"], "funds": b["mm"]["funds"]}}


# ---------------------------------------------------------------- propose → apply

class Proposals:
    def __init__(self, ttl_s: int = 120, clock=time.time):
        self.ttl, self.clock, self.items = int(ttl_s), clock, {}

    def add(self, kind: str, params: dict, preview: dict) -> dict:
        self._gc()
        cid = secrets.token_hex(4)
        exp = self.clock() + self.ttl
        self.items[cid] = {"kind": kind, "params": params, "preview": preview, "expires": exp}
        return {"confirmation_id": cid, "expires_at": dt.datetime.fromtimestamp(exp).astimezone().isoformat(timespec="seconds"),
                "expires_in_s": self.ttl}

    def peek(self, cid: str) -> dict:
        it = self.items.get(str(cid or ""))
        if not it:
            raise ActionError("confirmation_id desconocido o ya usado: proponé de nuevo", "unknown_proposal")
        if self.clock() > it["expires"]:
            self.items.pop(cid, None)
            raise ActionError("la propuesta venció: proponé de nuevo", "proposal_expired")
        return it

    def take(self, cid: str) -> dict:
        it = self.peek(cid)
        self.items.pop(cid, None)  # single use
        return it

    def _gc(self) -> None:
        now = self.clock()
        for k in [k for k, v in self.items.items() if now > v["expires"]]:
            self.items.pop(k, None)


def _guard_day(con, c: dict, t: dt.datetime) -> None:
    today = t.date().isoformat()
    errors = con.execute("select count(*) from decisions where day=? and status in ('error','unknown')", (today,)).fetchone()[0]
    sent = con.execute("select count(*) from decisions where day=? and status='sent'", (today,)).fetchone()[0]
    if errors >= int(c["max_errors_day"]) or sent >= int(c["max_orders_day"]):
        raise ActionError(f"circuit breaker: hoy hay {errors} error(es) y {sent} orden(es); límite alcanzado", "breaker")


def propose_caucion(cfg: dict, store: Proposals, bdays: int, amount: float, rate_pct: float | None = None,
                    api=None, live_flag: bool = False) -> dict:
    c = cfg["bot"]
    api = api or iolapi.make_client(cfg)
    t = config.now_local(cfg)
    try:
        bdays, amount = int(bdays), round(float(amount), 2)
    except (TypeError, ValueError):
        raise ActionError("bdays tiene que ser entero y amount un número", "invalid") from None
    con = bot.db()
    try:
        terms = bot.all_terms(api, c, t)
        rows = {r["bdays"]: r for r in bot.snapshot(api, con, c, t, terms=terms)}
        r = rows.get(bdays)
        if not r:
            raise ActionError(f"no hay un plazo de {bdays} día(s) hábil(es) permitido hoy (bot.terms_days)", "invalid")
        rate = float(rate_pct) if rate_pct is not None else r["take_rate"]
        if not rate or rate <= 0:
            raise ActionError("no hay tasa: el libro no tiene tomadores y no pasaste rate_pct", "invalid")
        if amount < bot.floor_amount(r, c):
            raise ActionError(f"el monto mínimo es {bot.floor_amount(r, c):.2f}", "invalid")
        if float(c["max_order_amount"] or 0) > 0 and amount > float(c["max_order_amount"]):
            raise ActionError("supera bot.max_order_amount", "invalid")
        free = bot.available(api, c, t)
        if amount > free:
            raise ActionError(f"supera los pesos libres (menos keep_ars): {free:.2f}", "invalid")
        fees = bot.fees_of(api.estimate(r["term_id"], amount, iolapi.to_api(rate, r["unit"]), c["currency"]))
    finally:
        con.close()
    net = bot.net_tna(amount, rate, r["cal_days"], fees)
    warnings = []
    if r["take_rate"] and rate > r["take_rate"]:
        warnings.append(f"tu límite ({rate:.2f}%) está arriba del mejor tomador ({r['take_rate']:.2f}%): puede no llenarse")
    if net <= float(c["min_net_tna"]):
        warnings.append("la comisión se come el interés: apply la va a saltear")
    live_ok = bot.is_live(cfg, live_flag)
    if not live_ok:
        warnings.append("modo simulado: apply no va a mandar nada mientras no esté habilitado el modo en vivo")
    preview = {"action": "colocar caución (colocadora, orden límite)", "currency": c["currency"], "bdays": bdays,
               "cal_days": r["cal_days"], "due_date": str(r["due_date"])[:10], "amount": amount, "limit_rate_pct": rate,
               "best_taker_now_pct": r["take_rate"], "fees_estimate": round(fees, 2), "net_tna_pct": round(net, 2),
               "interest_estimate": round(amount * rate / 100 * r["cal_days"] / 365 - fees, 2),
               "live_enabled": live_ok, "warnings": warnings}
    row = {k: r[k] for k in ("term_id", "term_days", "bdays", "cal_days", "due_date", "unit", "min_amount", "max_amount")}
    return {"preview": preview, **store.add("caucion", {"row": row, "amount": amount, "rate": rate}, preview)}


def propose_cancel(cfg: dict, store: Proposals, order_id: str, live_flag: bool = False) -> dict:
    t = config.now_local(cfg)
    con = bot.db()
    try:
        o = con.execute("select * from orders where order_id=? and status in ('sent','pending')", (str(order_id),)).fetchone()
    finally:
        con.close()
    if not o:
        raise ActionError("no hay una orden abierta con ese id registrada por caucionetta", "invalid")
    preview = {"action": "cancelar orden de caución", "order_id": o["order_id"], "placed_at": o["ts"],
               "amount": o["amount"], "rate_pct": o["rate"], "live_enabled": bot.is_live(cfg, live_flag),
               "at": t.isoformat(timespec="seconds")}
    return {"preview": preview, **store.add("cancel", {"order_id": o["order_id"]}, preview)}


def apply(cfg: dict, store: Proposals, confirmation_id: str, api=None, live_flag: bool = False) -> dict:
    store.peek(confirmation_id)  # unknown / expired → error, before anything else
    ok, why = config.live_allowed(cfg, live_flag)
    if not ok:
        raise ActionError(f"no ejecuté nada: {why}. Sólo la persona puede habilitar el modo en vivo.", "live_disabled")
    if bot.paused():
        raise ActionError("no ejecuté nada: el bot está pausado (`caucionetta bot resume` lo retoma)", "paused")
    it = store.take(confirmation_id)
    c = cfg["bot"]
    api = api or iolapi.make_client(cfg)
    t = config.now_local(cfg)
    con = bot.db()
    try:
        if it["kind"] == "caucion":
            _guard_day(con, c, t)
            p = it["params"]
            d = bot.place(api, con, c, t, "manual", p["row"], p["amount"], p["rate"], None,
                          "propuesta aprobada (propose → apply)", dry=False)
            return {"executed": d["status"] == "sent", "status": d["status"], "order_id": d["order_id"],
                    "error": d["error"], "net_tna_pct": d["net_tna"]}
        if it["kind"] == "cancel":
            o = con.execute("select * from orders where order_id=?", (it["params"]["order_id"],)).fetchone()
            if not o or o["status"] not in ("sent", "pending"):
                raise ActionError("la orden ya no está abierta", "stale_proposal")
            done = bot.cancel(api, con, o, t, dry=False)
            return {"executed": done, "order_id": o["order_id"]}
        raise ActionError(f"tipo de propuesta desconocido: {it['kind']}")
    finally:
        con.close()
