"""Demo mode: CAUCIONETTA_DEMO=1 (or `caucionetta demo`). Every number here is MADE UP.

A fictional broker (`DemoAPI`) with round amounts and rates, a fake fund ticker (DEMOMM), a frozen fictional
clock (Tuesday 2030-03-12 16:51) and its own state dir under the system temp folder. It never reads your config,
your Keychain or your state, and never touches the network. Used for the README's screenshots and examples, and
to try the CLI, the menu bar and the MCP server without an account.
"""
from __future__ import annotations

import datetime as dt
import shutil
import uuid

from . import bot, config, status

FUND = "DEMOMM"
RATES = {1: 32.0, 2: 32.5, 3: 33.0, 6: 33.5, 7: 34.0}   # calendar days → TNA %, fictional
FREE = 450_000.0
FUND_VALUE = 250_000.0
FUND_TNA = 31.0
FEE = 0.00005                                           # fictional fee per order (plus 21% VAT)


class DemoAPI:
    """Same methods as api.Client, answered from fictional data. Orders only change this object's memory."""

    def __init__(self):
        self.session = config.now_local().date()
        self.avail = FREE
        self.ops: dict = {}

    def token(self) -> str:
        return "demo"

    def forget_tokens(self) -> None:
        pass

    def terms(self, currency="ARS"):
        return [{"term_id": 9000 + d, "term_days": d, "due_date": (self.session + dt.timedelta(days=d)).isoformat(),
                 "min_amount": 1000, "max_amount": 0} for d in RATES]

    def quotes(self, term_id, currency="ARS"):
        r = RATES.get(int(term_id) - 9000)
        if r is None:
            return []
        return [{"position": 1, "ask": {"rate": r, "amount": 2_000_000}, "bid": {"rate": r + 1.5, "amount": 500_000}},
                {"position": 2, "ask": {"rate": r - 0.5, "amount": 5_000_000}, "bid": {"rate": r + 2.0, "amount": 1_000_000}}]

    def balances(self):
        return {"ars": {"available": {"t0": self.avail}, "committed": {"t0": 0}}}

    def positions(self):
        return [{"instrument": {"symbol": FUND}, "availability": {"quantity_available": FUND_VALUE / 10},
                 "market_value": {"current_price": 10}, "profit_loss": {"unrealized_pl": 1_500}}]

    def estimate(self, term_id, amount, rate_api, currency="ARS"):
        return [{"fee_type": "commission", "amount": round(amount * FEE, 2), "vat_amount": round(amount * FEE * 0.21, 2)}]

    def place(self, term_id, amount, rate_api, idem, currency="ARS"):
        uuid.UUID(idem)
        oid = f"DEMO-{len(self.ops) + 1}"
        self.ops[oid] = {"order_id": oid, "status": "pending", "executed": {"amount": 0}, "amount": amount}
        self.avail -= amount
        return oid

    def cancel(self, order_id):
        o = self.ops.get(order_id)
        if o and o["status"] == "pending":
            o["status"] = "cancelled"
            self.avail += o["amount"]
        return {"order_id": order_id, "cancelled": True}

    def operations(self, day, transaction_type="repo"):
        return list(self.ops.values())

    def fund_bars(self, symbol, start, end):
        out, p, d = [], 10.0, start
        while d <= end:
            out.append({"datetime": f"{d.isoformat()}T15:00:00.000Z", "close": round(p, 6)})
            p *= 1 + FUND_TNA / 100 / 365
            d += dt.timedelta(days=1)
        return out

    def fund_validate(self, kind, symbol, amount=None):
        return {"valid": True, "messages": []}

    def fund_subscribe(self, symbol, amount, idem):
        return f"DEMO-S-{uuid.UUID(idem).hex[:6]}"

    def fund_redeem_all(self, symbol, idem):
        return f"DEMO-R-{uuid.UUID(idem).hex[:6]}"

    def fund_cancel(self, tid):
        return {"cancelled": True}


def _weekdays_before(day: dt.date, n: int) -> list[dt.date]:
    out, d = [], day
    while len(out) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def seed(reset: bool = True) -> dict:
    """Builds the fictional day in the demo state dir and writes its status.json."""
    if not config.demo():
        raise RuntimeError("seed() only runs in demo mode (CAUCIONETTA_DEMO=1)")
    d = config.state_dir()
    if reset:
        shutil.rmtree(d, ignore_errors=True)
        d = config.state_dir()
    cfg = config.load()
    c = cfg["bot"]
    t = config.now_local(cfg)
    today = t.date()
    con = bot.db()
    # two weeks of ordinary readings, so the baselines come from "own" history
    for i, day in enumerate(_weekdays_before(today, 12)):
        for bd, cal in enumerate(RATES, start=1):
            for hh in ("11:00", "14:00", "16:30"):
                rate = RATES[cal] - 2.0 + (i % 3) * 0.5
                con.execute("insert into rates (ts, day, hhmm, term_id, term_days, cal_days, take_rate, bdays, high) "
                            "values (?,?,?,?,?,?,?,?,0)", (f"{day}T{hh}:00", day.isoformat(), hh, str(9000 + cal), cal,
                                                           cal, rate, bd))
    # today's readings: a spike on the 3-business-day term at 12:40, back to normal later
    for hh in ("10:35", "12:40", "15:30", "16:45"):
        for bd, cal in enumerate(RATES, start=1):
            rate = 48.0 if (hh == "12:40" and bd == 3) else RATES[cal]
            con.execute("insert into rates (ts, day, hhmm, term_id, term_days, cal_days, due_date, take_rate, offer_rate,"
                        " bdays, high) values (?,?,?,?,?,?,?,?,?,?,0)",
                        (f"{today}T{hh}:00", today.isoformat(), hh, str(9000 + cal), cal, cal,
                         (today + dt.timedelta(days=cal)).isoformat(), rate, rate + 1.5, bd))
    # yesterday's 1-day caución (already due: counts as earned) and today's spike (filled)
    yday = _weekdays_before(today, 1)[0]
    rows = [("d-yday", f"{yday}T16:50:00", yday, "final", 1, 1, 400_000.0, 31.0, 30.0, round(400_000 * FEE * 1.21, 2),
             "pesos libres al cierre: el mejor plazo corto es 1 día hábil", "DEMO-Y1", (yday + dt.timedelta(days=1))),
            ("d-spike", f"{today}T12:40:00", today, "spike", 3, 3, 300_000.0, 46.5, 31.0, round(300_000 * FEE * 1.21, 2),
             "pico: 3 días hábiles paga 48% contra 31% de referencia", "DEMO-T1", (today + dt.timedelta(days=3)))]
    for did, ts, day, kind, bd, cal, amount, rate, base, fees, why, oid, due in rows:
        con.execute("insert into decisions (id, ts, day, kind, term_days, term_id, cal_days, amount, rate, baseline, fees,"
                    " net_tna, reason, status, order_id, dry, error, bdays) values (?,?,?,?,?,?,?,?,?,?,?,?,?,'sent',?,0,NULL,?)",
                    (did, ts, day.isoformat(), kind, cal, str(9000 + cal), cal, amount, rate, base, fees,
                     bot.net_tna(amount, rate, cal, fees), why, oid, bd))
        con.execute("insert into orders (order_id, decision_id, ts, day, term_days, cal_days, due_date, amount, rate,"
                    " status, filled, checked, kind) values (?,?,?,?,?,?,?,?,?,'completed',?,?,?)",
                    (oid, did, ts, day.isoformat(), cal, cal, due.isoformat(), amount, rate, amount, ts, kind))
    con.commit()
    config.write_state({"rate_unit": "percent", "last_poll": (t - dt.timedelta(minutes=6)).isoformat(timespec="seconds"),
                        "mm_yields": {"day": today.isoformat(), "data": {FUND: FUND_TNA}},
                        "heartbeat": {"at": t.isoformat(timespec="seconds"), "ok": True,
                                      "summary": "5 plazos · 0 pico(s) · 0 orden(es) · simulado (dry-run)"}})
    api = DemoAPI()
    flags = bot.day_flags(bot.all_terms(api, c, t), t)
    status.refresh(api, con, c, t, "abierto", flags, force=True, dry=True)
    con.close()
    return status.read() or {}


def ensure_seeded() -> None:
    if config.demo() and not (config.state_dir() / "status.json").exists():
        seed(reset=True)
