"""The status file the menu bar reads: <state>/status.json (0600).

The bot rewrites it at most every few minutes (and at once after it acts), so opening the menu bar never calls
IOL. It holds your money (free, in caución, in a fund, total), live cauciones with their expected interest, what
it earns, today's actions, the rates now against their baseline and the bot's mode. It's yours and local: don't
paste it anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import api as iolapi
from . import bot, config

KIND = {"spike": "Pico", "rest": "Espera en el libro", "final": "Cierre", "manual": "Manual", "mm_in": "Al fondo",
        "mm_out": "Rescate"}
STATUS = {"filled": "colocada", "unfilled": "no se llenó (cancelada)", "sent": "enviada", "dry": "simulada",
          "error": "rechazada", "skipped": "salteada", "unknown": "sin respuesta", "cancelled": "cancelada",
          "planned": "pensada"}
EVERY_MIN = 5
VERSION = 1


def path():
    return config.state_dir() / "status.json"


def read() -> dict | None:
    try:
        return json.loads(path().read_text())
    except (OSError, ValueError):
        return None


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _live_cauciones(con, t: dt.datetime) -> list[dict]:
    today = t.date().isoformat()
    out = []
    for o in con.execute("select o.*, d.fees from orders o left join decisions d on d.id=o.decision_id "
                         "where coalesce(o.kind,'take')!='rest' and date(substr(o.due_date,1,10)) > date(?) and "
                         "(o.status='completed' or coalesce(o.filled,0)>0 or (o.status in ('sent','pending') and o.day=?))",
                         (today, today)).fetchall():
        amount = _f(o["filled"]) or _f(o["amount"])
        days = int(o["cal_days"] or 1)
        gross = amount * _f(o["rate"]) / 100 * days / 365
        out.append({"amount": round(amount, 2), "rate": _f(o["rate"]), "due": str(o["due_date"])[:10], "days": days,
                    "interest": round(gross - _f(o["fees"]), 2), "filled": o["status"] == "completed" or _f(o["filled"]) > 0})
    return sorted(out, key=lambda x: x["due"])


def _earned(con, t: dt.datetime) -> dict:
    """Interest of the cauciones already due (net of IOL's estimated fees): this month and since the start."""
    month = t.strftime("%Y-%m")
    tot = mon = 0.0
    for o in con.execute("select o.*, d.fees from orders o left join decisions d on d.id=o.decision_id where "
                         "(o.status='completed' or coalesce(o.filled,0)>0) and date(substr(o.due_date,1,10)) <= date(?)",
                         (t.date().isoformat(),)).fetchall():
        amount = _f(o["filled"]) or _f(o["amount"])
        net = amount * _f(o["rate"]) / 100 * int(o["cal_days"] or 1) / 365 - _f(o["fees"])
        tot += net
        if str(o["due_date"])[:7] == month:
            mon += net
    return {"cauciones_month": round(mon, 2), "cauciones_total": round(tot, 2)}


def _today(con, t: dt.datetime) -> list[dict]:
    rows = []
    for d in con.execute("select d.ts, d.kind, d.bdays, d.term_days, d.amount, d.rate, d.reason, d.error, "
                         "case when o.order_id is null then d.status when o.status='completed' or coalesce(o.filled,0)>0 "
                         "then 'filled' when o.status='cancelled' then 'unfilled' else d.status end status "
                         "from decisions d left join orders o on o.decision_id=d.id "
                         "where d.day=? order by d.ts", (t.date().isoformat(),)).fetchall():
        plazo = "" if d["kind"] in ("mm_in", "mm_out") else \
            bot.habiles(d['bdays'] or d['term_days']) + (f" ({d['term_days']} corridos)" if d["bdays"] and d["term_days"] != d["bdays"] else "")
        rows.append({"time": d["ts"][11:16], "kind": KIND.get(d["kind"], d["kind"]), "term": plazo,
                     "rate": _f(d["rate"]) or None, "amount": round(_f(d["amount"]), 2),
                     "status": STATUS.get(d["status"], d["status"]), "ok": d["status"] in ("sent", "dry", "filled"),
                     "why": (d["reason"] or "")[:160], "error": (d["error"] or "")[:120] or None})
    return rows


def _rates(con, c: dict, t: dt.datetime) -> list[dict]:
    out = []
    last = con.execute("select max(ts) from rates where day=?", (t.date().isoformat(),)).fetchone()[0]
    if not last:
        return out
    for r in con.execute("select bdays, term_days, take_rate, offer_rate from rates where ts=? and bdays is not null "
                         "order by bdays", (last,)).fetchall():
        h = bot.history(con, r["bdays"], t, c)
        out.append({"bdays": r["bdays"], "days": r["term_days"], "take": r["take_rate"], "offer": r["offer_rate"],
                    "ref": h.get("median"), "ref_source": h.get("source")})
    return out


def mode_label(dry: bool, t: dt.datetime) -> str:
    if bot.paused():
        return "pausado"
    if bot._state().get("breaker") == t.date().isoformat():
        return "frenado hoy (seguridad)"
    return "simulado" if dry else "en vivo"


def build(con, c: dict, t: dt.datetime, money: dict | None, fund: dict | None, flags: dict | None,
          market: str, dry: bool = True) -> dict:
    st = bot._state()
    live = _live_cauciones(con, t)
    in_cau = sum(x["amount"] for x in live)
    mm = (st.get("mm_yields") or {}).get("data") or {}
    fund = fund or {}
    fund_val = _f(fund.get("value"))
    fund_tna = _f(mm.get(fund.get("symbol"))) if fund.get("symbol") else (max(mm.values()) if mm else 0.0)
    free = _f((money or {}).get("available"))
    # a subscription sent today runs later: until then IOL shows it as «committed» and the fund isn't in the
    # positions yet. Count it as on its way to the fund.
    fund_pending = 0.0 if fund_val > 0 else min(_f((money or {}).get("committed")), bot.parked_today(con, t))
    if fund_pending and not fund.get("symbol"):
        fund = dict(fund, symbol=(con.execute("select term_id from decisions where day=? and kind='mm_in' and status in "
                                              "('sent','dry') order by ts desc", (t.date().isoformat(),)).fetchone() or [None])[0])
        fund_tna = _f(mm.get(fund.get("symbol"))) if fund.get("symbol") else fund_tna
    fund_val += fund_pending
    total = free + in_cau + fund_val
    per_day = sum(x["amount"] * x["rate"] / 100 / 365 for x in live) + fund_val * fund_tna / 100 / 365
    working = in_cau + fund_val
    flags = flags or {}
    kind = ("cierre bancario de %d días" % flags["gap"]) if flags.get("long_closure") else \
        "fin de mes" if flags.get("month_end") else "temporada de fin de año" if flags.get("season") else \
        "viernes" if flags.get("friday") else "normal"
    hb = st.get("heartbeat") or {}
    return {
        "v": VERSION, "at": t.isoformat(timespec="seconds"), "market": market, "mode": mode_label(dry, t),
        "demo": bool(os.environ.get("CAUCIONETTA_DEMO")),
        "bot": {"ok": hb.get("ok"), "summary": hb.get("summary"), "at": hb.get("at")},
        "day": {"kind": kind, "high": bool(flags.get("high")), "final_at": bot.final_time(c, flags) if flags else None},
        "money": {"total": round(total, 2), "free": round(free, 2), "in_caucion": round(in_cau, 2),
                  "in_fund": round(fund_val, 2), "in_fund_pending": round(fund_pending, 2),
                  "money_at": (money or {}).get("at")},
        "yield": {"working_tna": round(per_day * 365 / working * 100, 2) if working else None,
                  "per_day": round(per_day, 2), "month_projection": round(per_day * 30, 2),
                  "fund_tna": round(fund_tna, 2) if fund_tna else None},
        "earned": dict(_earned(con, t), fund_unrealized=round(_f(fund.get("pl")), 2) if fund else 0.0),
        "cauciones": live,
        "fund": {"symbol": fund.get("symbol"), "name": fund.get("symbol"),
                 "value": round(fund_val, 2), "tna": round(fund_tna, 2) if fund_tna else None,
                 "pending": round(fund_pending, 2)},
        "today": _today(con, t),
        "rates": _rates(con, c, t),
        "summary": (con.execute("select summary from daily where day=?", (t.date().isoformat(),)).fetchone() or [None])[0],
    }


def fetch_money(api, c: dict) -> tuple[dict, dict]:
    """(money, fund) from IOL: two calls (balance and positions)."""
    b = api.balances() or {}
    ars = b.get("ars") or {}
    money = {"available": _f((ars.get("available") or {}).get("t0")), "committed": _f((ars.get("committed") or {}).get("t0")),
             "at": config.now_local().isoformat(timespec="seconds")}
    fund = {}
    for pos in api.positions() or []:
        sym = ((pos.get("instrument") or {}).get("symbol")) or pos.get("symbol")
        if sym in c["mm"]["funds"]:
            av = pos.get("availability") or {}
            qty = _f(av.get("quantity_available")) + _f(av.get("quantity_pending"))
            price = _f((pos.get("market_value") or {}).get("current_price"))
            fund = {"symbol": sym, "value": qty * price, "pl": _f((pos.get("profit_loss") or {}).get("unrealized_pl"))}
    return money, fund


def write(data: dict) -> None:
    config.atomic_write(path(), json.dumps(data, ensure_ascii=False, indent=1), mode=0o600)


def refresh(api, con, c: dict, t: dt.datetime, market: str, flags: dict | None = None, force: bool = False,
            dry: bool = True) -> bool:
    """Rewrites the status (2 IOL calls) at most every EVERY_MIN minutes, or always with `force`."""
    old = read() or {}
    at = old.get("at")
    if not force and at and (t - dt.datetime.fromisoformat(at)).total_seconds() < EVERY_MIN * 60 - 20:
        return False
    if flags is None:
        try:
            flags = bot.day_flags(bot.all_terms(api, c, t), t)
        except iolapi.IOLError:
            flags = {}
    try:
        money, fund = fetch_money(api, c)
    except iolapi.IOLError:
        om, of = old.get("money"), old.get("fund")
        money = {"available": om.get("free"), "at": om.get("money_at")} if om else None
        fund = {"symbol": of.get("symbol"), "value": of.get("value")} if of else {}
    write(build(con, c, t, money, fund, flags, market, dry=dry))
    return True
