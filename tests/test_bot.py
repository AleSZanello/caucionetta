"""The caución bot against a fake IOL: closed endpoint list, rate units, business-day terms, day flags, spikes
vs history (own or A3), the resting order on high-probability days, the final placement, stress streaks, fees,
idempotency, cancelling stale orders, notifications without amounts. No network, no real account; every date and
amount is fictional."""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from caucionetta import a3, bot, credentials, notify, status
from caucionetta import api as iolapi

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def cycle(api, con, c, t, dry=False):
    """These tests exercise the decision logic against a fake broker, so they run the cycle live on purpose
    (the library default is dry=True; see test_safety.py)."""
    return bot.cycle(api, con, c, t, dry=dry)


def track(api, con, c, t, dry=False):
    return bot.track(api, con, c, t, dry=dry)


def tna_14d(x: float) -> float:
    """The fake fund compounds daily, so its 14-day TNA is a hair above the daily rate."""
    return round(((1 + x / 100 / 365) ** 14 - 1) * 365 / 14 * 100, 2)


T = dt.datetime(2030, 3, 12, 11, 0, tzinfo=TZ)  # a fictional Tuesday, market open


class FakeAPI:
    def __init__(self, rates: dict, avail: float = 1_000_000, fraction: bool = False, depth: float = 10_000_000,
                 days=(1, 2, 3, 6, 7, 14), session: dt.date = T.date(), mm_tna: dict | None = None,
                 holdings: dict | None = None, levels: dict | None = None, inverted: bool = False, fill: bool = False):
        self.rates, self.avail, self.fraction, self.depth = rates, avail, fraction, depth
        self.days, self.session = days, session
        self.levels, self.inverted, self.fill = levels or {}, inverted, fill
        self.fail_next: list = []  # exceptions the next place() calls raise
        self.mm_tna = mm_tna or {}  # {symbol: TNA %}: no funds → no money market
        self.holdings = dict(holdings or {})  # {symbol: value}
        self.subs: list = []
        self.redeems: list = []
        self.fund_cancels: list = []
        self.placed: list = []
        self.cancelled: list = []
        self.ops: dict = {}
        self.calls: list = []

    def r(self, v):
        return v / 100 if self.fraction else v

    def terms(self, currency):
        self.calls.append("terms")
        return [{"term_id": 100 + d, "term_days": d, "due_date": (self.session + dt.timedelta(days=d)).isoformat(),
                 "min_amount": 100, "max_amount": 0} for d in self.days]

    def quotes(self, term_id, currency):
        d = term_id - 100
        if d in self.levels:  # several takers: (rate, amount), best first
            return [{"position": i + 1, "ask": {"rate": self.r(rt), "amount": am},
                     "bid": {"rate": self.r(rt + 5), "amount": 5_000}} for i, (rt, am) in enumerate(self.levels[d])]
        rate = self.rates.get(d)
        gap = -2 if self.inverted else 2  # a resting book: the lenders ask more than the takers pay
        return [{"position": 1, "bid": {"rate": self.r((rate or 30) + gap), "amount": 5_000},
                 "ask": {"rate": self.r(rate) if rate else None, "amount": self.depth}}]

    def balances(self):
        return {"ars": {"available": {"t0": self.avail}}}

    def estimate(self, term_id, amount, rate_api, currency):
        return [{"fee_type": "commission", "amount": amount * 0.0005, "vat_amount": amount * 0.0005 * 0.21}]

    def place(self, term_id, amount, rate_api, idem, currency):
        uuid.UUID(idem)
        if self.fail_next:
            raise self.fail_next.pop(0)
        if any(p["idem"] == idem for p in self.placed):  # IOL's idempotency: the same key never creates a second
            return next(oid for oid, o in self.ops.items() if o.get("idem") == idem)
        self.placed.append({"term_id": term_id, "amount": amount, "rate": rate_api, "idem": idem})
        self.avail -= amount
        oid = f"o{len(self.placed)}"
        self.ops[oid] = {"order_id": oid, "status": "completed" if self.fill else "pending",
                         "executed": {"amount": amount if self.fill else 0}, "amount": amount, "idem": idem}
        return oid

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        o = self.ops.get(order_id)
        if o and o["status"] == "pending":
            o["status"] = "cancelled"
            self.avail += o["amount"]
        return {"order_id": order_id, "status": "cancelled", "cancelled": True}

    def operations(self, day):
        return list(self.ops.values())

    # money market (closed list enforced by the real client)
    def fund_bars(self, symbol, start, end):
        tna = self.mm_tna.get(symbol)
        if tna is None:
            return []
        out, p, d = [], 100.0, start
        while d <= end:
            out.append({"datetime": f"{d.isoformat()}T15:00:00.000Z", "close": p})
            p *= 1 + tna / 100 / 365
            d += dt.timedelta(days=1)
        return out

    def positions(self):
        return [{"instrument": {"symbol": k}, "availability": {"quantity_available": v / 10},
                 "market_value": {"current_price": 10}} for k, v in self.holdings.items() if v > 0]

    def fund_validate(self, kind, symbol, amount=None):
        return {"valid": True, "messages": []}

    def fund_subscribe(self, symbol, amount, idem):
        uuid.UUID(idem)
        self.subs.append((symbol, amount))  # the balance doesn't show it until the evening (worst case)
        return f"s{len(self.subs)}"

    def fund_redeem_all(self, symbol, idem):
        uuid.UUID(idem)
        self.redeems.append(symbol)
        self.avail += self.holdings.pop(symbol, 0)  # T+0: credited at once
        return f"r{len(self.redeems)}"

    def fund_cancel(self, tid):
        self.fund_cancels.append(tid)
        return {"cancelled": True}


class IOLTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "state").mkdir()
        self.notes: list = []
        self.patches = [mock.patch.dict(os.environ, {"CAUCIONETTA_STATE_DIR": str(d / "state")}),
                        mock.patch.object(notify, "send", lambda *a, **k: self.notes.append((a, k)))]
        for p in self.patches:
            p.start()
        self.cfg = {"live": True, "bot": {"spike": {"min_days": 3}, "mm": {"enabled": True, "funds": ["FONDOA", "FONDOB"]}}}
        self.c = bot.conf(self.cfg)
        self.con = bot.db()

    def tearDown(self):
        self.con.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def seed_history(self, bdays: int, rate: float, n: int = 10):
        for i in range(n):
            day = (T.date() - dt.timedelta(days=1 + i % 5)).isoformat()
            self.con.execute("insert into rates (ts, day, hhmm, term_id, term_days, take_rate, bdays) values (?,?,?,?,?,?,?)",
                             (day + "T11:00:00", day, "11:00", "x", bdays, rate, bdays))
        self.con.commit()

    def seed_a3(self, bdays: int, pp: float, n: int = 6, before: dt.date = T.date()):
        for i in range(n):
            day = (before - dt.timedelta(days=1 + i)).isoformat()
            self.con.execute("insert or replace into a3_daily values (?,?,?,?,?,?,?,?,?)",
                             (day, bdays, day, bdays, pp, pp * 1.2, pp * 0.8, pp, 1e9))
        self.con.commit()

    # -------------------------------------------------------------- the client
    def test_closed_endpoint_list(self):
        self.assertTrue(iolapi.allowed("POST", "/broker/repos"))
        self.assertTrue(iolapi.allowed("DELETE", "/broker/repos/abc-123"))
        self.assertTrue(iolapi.allowed("GET", "/broker/market-data/repos/quotes"))
        for m, p in (("POST", "/broker/orders"), ("POST", "/broker/repos-borrowing"), ("GET", "/broker/withdrawals"),
                     ("POST", "/broker/transfers"), ("GET", "/broker/accounts/US/balances"), ("POST", "/broker/fci")):
            self.assertFalse(iolapi.allowed(m, p), p)
        with self.assertRaises(iolapi.IOLError):
            iolapi.Client()._http("POST", "/broker/orders", body={})

    def test_rate_units(self):
        self.assertEqual(iolapi.pct(45.5), 45.5)
        self.assertAlmostEqual(iolapi.pct(0.455), 45.5)
        self.assertAlmostEqual(iolapi.pct(1.9), 190.0)
        self.assertIsNone(iolapi.pct(None))
        self.assertEqual(iolapi.to_api(190, 0.4), 1.9)
        self.assertEqual(iolapi.to_api(190, 45), 190)

    def test_the_call_cap_only_warns_once(self):
        with mock.patch.object(iolapi, "_calls", lambda m: {"month": m, "n": 99}):
            iolapi.count_call("/broker/market-data/repos/terms", 50)  # never blind: it keeps reading
        self.assertTrue(any("tope de llamadas" in n[0][0] for n in self.notes))

    def test_a_rejected_password_is_not_retried_every_minute(self):
        cl = iolapi.Client()
        with mock.patch.object(credentials, "get", lambda *a, **k: ("x", "x")), \
                mock.patch.object(cl, "_http", side_effect=iolapi.IOLError("401", 401)) as http:
            with self.assertRaises(iolapi.IOLError):
                cl.token()
            with self.assertRaises(iolapi.IOLError) as e:
                cl.token()
        self.assertEqual(http.call_count, 1, "the second time it doesn't even ask IOL")
        self.assertEqual(e.exception.code, "auth_failed")

    def test_a_revoked_access_token_is_dropped_and_retried_once(self):
        cl = iolapi.Client()
        iolapi._save_private(cl.tok_path, {"access_token": "old", "access_until": "2999-01-01T00:00:00+00:00",
                                           "refresh_token": "r", "refresh_until": "2999-01-01T00:00:00+00:00"})
        calls = []

        def http1(method, path, params=None, body=None, headers=None, auth=True):
            calls.append(path)
            if path == "/auth/token":
                return {"access_token": "new", "expires_in": 900}
            if cl.token() == "old":
                raise iolapi.IOLError("401", 401)
            return {"ars": {}}
        with mock.patch.object(cl, "_http1", side_effect=http1):
            self.assertEqual(cl.balances(), {"ars": {}})
        self.assertEqual((calls.count("/broker/accounts/AR/balances"), calls.count("/auth/token")), (2, 1),
                         "one retry, with a fresh token from the refresh token")
        with mock.patch.object(cl, "_http1", side_effect=iolapi.IOLError("401", 401)) as h:
            with self.assertRaises(iolapi.IOLError):
                cl.balances()
        self.assertLessEqual(h.call_count, 3, "never a loop")

    # -------------------------------------------------------------- terms and flags
    def test_business_days_and_allowed_terms(self):
        api = FakeAPI({1: 36, 14: 400})
        rows = bot.snapshot(api, self.con, self.c, T)
        self.assertEqual([(r["term_days"], r["bdays"]) for r in rows], [(1, 1), (2, 2), (3, 3), (6, 4), (7, 5)])
        # before a 5-day bank closure the 1-business-day term spans 6 calendar days, and it's always allowed
        api = FakeAPI({}, days=(9, 10, 13))
        rows = bot.snapshot(api, self.con, self.c, T + dt.timedelta(days=1))
        self.assertEqual([(r["term_days"], r["bdays"]) for r in rows], [(9, 1)])

    def test_day_flags_from_the_terms(self):
        f = bot.day_flags(bot.all_terms(FakeAPI({}), self.c, T), T)
        self.assertFalse(f["high"])
        thu = dt.date(2030, 3, 14)  # a Thursday before a 5-day closure: next business day 20/3
        f = bot.day_flags([{"term_days": 6, "due_date": "2030-03-20", "bdays": 1}], dt.datetime(2030, 3, 14, 11, tzinfo=TZ))
        self.assertTrue(f["long_closure"] and f["high"])
        f = bot.day_flags([{"term_days": 1, "due_date": "2030-04-01", "bdays": 1}], T)
        self.assertTrue(f["month_end"])
        f = bot.day_flags([{"term_days": 3, "due_date": "2030-03-18", "bdays": 1}], T)
        self.assertTrue(f["friday"] and not f["high"])
        self.assertEqual(bot.final_time(self.c, f), "16:25", "ordinary Friday: the close tends to be lower")
        f = bot.day_flags([{"term_days": 1, "due_date": "2030-12-17", "bdays": 1}], T)
        self.assertTrue(f["season"])
        self.assertEqual(thu.weekday(), 3)

    # -------------------------------------------------------------- spikes
    def test_a_spike_against_its_business_day_history_takes_the_free_pesos(self):
        self.seed_history(4, 38)
        self.seed_history(1, 35)
        api = FakeAPI({1: 36, 2: 37, 3: 38, 6: 190, 7: 40})
        out = cycle(api, self.con, self.c, T)
        self.assertEqual(out["placed"], 1)
        self.assertEqual(api.placed[0]["term_id"], 106)
        self.assertEqual(api.placed[0]["rate"], 57.0, "limit at the threshold (1.5× 38): it trades at the takers' 190%")
        self.assertEqual(api.placed[0]["amount"], 1_000_000)
        title, sub = self.notes[-1][0][:2]
        self.assertIn("190%", title)
        self.assertNotIn("1000000", title + sub)
        self.assertNotIn("1.000.000", title + sub)

    def test_a_long_closure_term_is_the_1_business_day_term(self):
        """190% «a 6 días» before a 5-day closure: compared with the 1-business-day baseline (A3), not a 6-day one."""
        t = dt.datetime(2030, 3, 14, 11, 0, tzinfo=TZ)
        self.seed_a3(1, 30, before=t.date())
        self.seed_a3(4, 200, before=t.date())  # a normal 6-day term's history must not be the reference here
        api = FakeAPI({6: 190}, days=(6, 7, 8), session=t.date())
        cycle(api, self.con, self.c, t)
        self.assertEqual([p["term_id"] for p in api.placed], [106])

    def test_without_history_a_longer_term_far_above_1_day_is_a_spike(self):
        api = FakeAPI({1: 40, 6: 120})
        cycle(api, self.con, self.c, T)
        self.assertEqual([p["term_id"] for p in api.placed], [106])

    def test_a_spike_without_free_pesos_is_remembered_once(self):
        self.seed_history(4, 38)
        api = FakeAPI({1: 36, 6: 190}, avail=0)
        cycle(api, self.con, self.c, T)
        cycle(api, self.con, self.c, T + dt.timedelta(minutes=5))
        self.assertEqual(self.con.execute("select count(*) from notes where kind='missed_spike'").fetchone()[0], 1)
        self.assertEqual(api.placed, [])

    # -------------------------------------------------------------- the close
    def test_no_spike_nothing_happens_until_the_final_placement(self):
        api = FakeAPI({1: 36, 2: 37, 3: 38, 6: 39, 7: 40}, fraction=True)
        cycle(api, self.con, self.c, T)
        cycle(api, self.con, self.c, T.replace(hour=16, minute=40))
        self.assertEqual(api.placed, [], "waits: spikes come in the last minutes")
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        self.assertEqual(len(api.placed), 1)
        p = api.placed[0]
        self.assertEqual(p["term_id"], 101, "1 business day: longer ones don't pay enough more to be locked")
        self.assertAlmostEqual(p["rate"], 0.36, msg="the rate goes back in the unit the book used")

    def test_a_bridge_day_rests_orders_and_swaps_them_at_16_55(self):
        t = dt.datetime(2030, 3, 21, 14, 30, tzinfo=TZ)
        self.seed_a3(1, 25, before=t.date())  # a Thursday before a long weekend
        api = FakeAPI({5: 26, 6: 27, 7: 28}, days=(5, 6, 7), session=t.date())  # 1 business day = 5 calendar days
        out = cycle(api, self.con, self.c, t)
        self.assertTrue(out["flags"]["high"])
        rest = [p for p in api.placed]
        self.assertEqual([p["rate"] for p in rest], [55.0, 85.0], "2× and 3× the A3 baseline (25%), +30/+60 pp")
        self.assertAlmostEqual(sum(p["amount"] for p in rest), 1_000_000)
        cycle(api, self.con, self.c, t.replace(hour=15, minute=0))
        self.assertEqual(len(api.placed), 2, "once a day; they wait")
        cycle(api, self.con, self.c, t.replace(hour=16, minute=46))
        self.assertEqual(api.cancelled, [], "before a long closure the last minutes are the best: still waiting")
        cycle(api, self.con, self.c, t.replace(hour=16, minute=56))
        self.assertEqual(sorted(api.cancelled), ["o1", "o2"], "at 16:55 what didn't fill is cancelled")
        self.assertEqual(api.placed[-1]["term_id"], 105, "and the pesos go to the best caución now")
        self.assertAlmostEqual(api.placed[-1]["amount"], 1_000_000)

    def test_a_stress_streak_keeps_rolling_at_1_business_day(self):
        self.seed_a3(1, 25)
        t = T.replace(hour=16, minute=51)
        self.con.execute("insert into rates (ts, day, hhmm, term_days, take_rate, bdays) values (?,?,?,?,?,?)",
                         (T.isoformat(), T.date().isoformat(), "12:00", 1, 40, 1))
        self.con.commit()
        api = FakeAPI({1: 40, 2: 50})
        cycle(api, self.con, self.c, t)
        self.assertEqual(api.placed[-1]["term_id"], 101, "today's 40% is 1.6× its baseline: don't lock 2 days")

    def test_fees_that_eat_the_interest_skip_the_order(self):
        api = FakeAPI({1: 36})
        api.estimate = lambda *a: [{"amount": 10_000, "vat_amount": 2_100}]
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        self.assertEqual(api.placed, [])
        self.assertEqual(self.con.execute("select status from decisions").fetchone()["status"], "skipped")

    def test_dry_mode_decides_but_never_sends(self):
        self.seed_history(4, 38)
        api = FakeAPI({1: 36, 6: 190})
        cycle(api, self.con, self.c, T, dry=True)
        self.assertEqual(api.placed, [])
        self.assertEqual(self.con.execute("select status from decisions").fetchone()[0], "dry")

    def test_a_closing_order_still_pending_is_cancelled_after_2_minutes(self):
        api = FakeAPI({1: 36})
        cycle(api, self.con, self.c, T.replace(hour=16, minute=50))
        self.assertEqual(len(api.placed), 1)
        track(api, self.con, self.c, T.replace(hour=16, minute=51))
        self.assertEqual(api.cancelled, [])
        track(api, self.con, self.c, T.replace(hour=16, minute=53))
        self.assertEqual(api.cancelled, ["o1"], "a closing order gets 2 minutes, then it's placed again lower")
        self.assertEqual(self.con.execute("select status from orders").fetchone()[0], "cancelled")

    # -------------------------------------------------------------- money market parking
    def test_a_quiet_day_parks_the_pesos_in_the_best_same_day_fund(self):
        self.seed_a3(1, 21)
        api = FakeAPI({1: 21, 2: 22}, mm_tna={"FONDOA": 21.0, "FONDOB": 18.5})
        cycle(api, self.con, self.c, T.replace(hour=10, minute=40))
        self.assertEqual(api.subs, [("FONDOA", 1_000_000)], "21% beats ~18.6% net of the caución")
        self.assertIn("FONDOA", self.notes[-1][0][0])
        self.assertNotIn("1000000", self.notes[-1][0][0] + self.notes[-1][0][1])
        cycle(api, self.con, self.c, T.replace(hour=11, minute=40))
        self.assertEqual(len(api.subs), 1, "never twice: the balance may still show the pending subscription")
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        self.assertEqual(api.placed, [], "and those pesos aren't put in a caución at the close")

    def test_no_parking_when_todays_caucion_is_above_its_baseline(self):
        self.seed_a3(1, 21)  # baseline 21 → net 18.6 < fund 19.8, but today the 1-day term pays 24 (net 21.6)
        api = FakeAPI({1: 24}, mm_tna={"FONDOA": 19.8})
        cycle(api, self.con, self.c, T.replace(hour=10, minute=50))
        self.assertEqual(api.subs, [])

    def test_no_parking_when_the_caucion_pays_more_or_after_14_45(self):
        self.seed_a3(1, 30)
        api = FakeAPI({1: 30}, mm_tna={"FONDOA": 21.0})
        cycle(api, self.con, self.c, T.replace(hour=10, minute=40))
        self.assertEqual(api.subs, [], "30% − 2.4 beats the fund")
        self.con.execute("delete from a3_daily")
        self.seed_a3(1, 18)
        api = FakeAPI({1: 18}, mm_tna={"FONDOA": 21.0})
        cycle(api, self.con, self.c, T.replace(hour=15, minute=0))
        self.assertEqual(api.subs, [], "after 14:45 the order would run tomorrow")

    def test_a_high_probability_day_rescues_the_fund_in_the_morning(self):
        t = dt.datetime(2030, 3, 21, 10, 40, tzinfo=TZ)
        self.seed_a3(1, 21, before=t.date())
        api = FakeAPI({5: 22}, days=(5, 6, 7), session=t.date(), avail=0, mm_tna={"FONDOA": 21.0},
                      holdings={"FONDOA": 500_000})
        cycle(api, self.con, self.c, t)
        self.assertEqual(api.redeems, ["FONDOA"])
        self.assertEqual(api.subs, [])
        self.assertEqual(api.avail, 500_000, "T+0: ready for the resting orders at 14:00")

    def test_a_spike_rescues_the_parked_pesos_at_once(self):
        self.seed_a3(1, 21)
        self.seed_a3(4, 22)
        api = FakeAPI({1: 21, 6: 120}, avail=0, mm_tna={"FONDOA": 21.0}, holdings={"FONDOA": 400_000})
        cycle(api, self.con, self.c, T.replace(hour=12))
        self.assertEqual(api.redeems, ["FONDOA"])
        self.assertEqual([(p["term_id"], p["amount"]) for p in api.placed], [(106, 400_000)])

    def test_a_spike_cancels_todays_pending_subscription(self):
        self.seed_a3(1, 21)
        self.seed_a3(4, 22)
        api = FakeAPI({1: 21, 6: 30}, mm_tna={"FONDOA": 21.0})
        cycle(api, self.con, self.c, T.replace(hour=10, minute=40))
        self.assertEqual(len(api.subs), 1)
        api.rates[6] = 120  # a spike after parking
        cycle(api, self.con, self.c, T.replace(hour=12))
        self.assertEqual(api.fund_cancels, ["s1"])
        self.assertEqual([p["term_id"] for p in api.placed], [106])

    def test_a_stale_last_price_does_not_dilute_the_yield(self):
        api = FakeAPI({}, mm_tna={"FONDOA": 21.0})
        bars = api.fund_bars("FONDOA", T.date() - dt.timedelta(days=21), T.date() - dt.timedelta(days=5))
        bars += [dict(bars[-1], datetime=f"{(T.date() - dt.timedelta(days=i)).isoformat()}T00:00:00.000Z")
                 for i in (4, 0)]  # a bank holiday: the same price repeated
        api.fund_bars = lambda *a: bars
        self.assertAlmostEqual(bot.mm_yields(api, self.con, self.c, T)["FONDOA"], 21.0, delta=0.2)

    def test_only_same_day_money_market_funds(self):
        with self.assertRaises(iolapi.IOLError):
            iolapi.Client(funds=["FONDOA"])._fund("OTROFONDO")  # not in the configured list
        self.assertTrue(iolapi.allowed("POST", "/broker/mutual-funds/subscriptions"))
        self.assertFalse(iolapi.allowed("GET", "/broker/market-data/GGAL/bars", ["FONDOA"]))
        self.assertTrue(iolapi.allowed("GET", "/broker/market-data/FONDOA/bars", ["FONDOA"]))
        self.assertEqual(bot.mm_yields(FakeAPI({}, mm_tna={"FONDOA": 21.0}), self.con, self.c, T)["FONDOA"],
                         tna_14d(21.0))

    # -------------------------------------------------------------- guards
    def test_no_trading_on_a_weekday_holiday(self):
        api = FakeAPI({1: 36}, session=T.date() + dt.timedelta(days=1))  # the terms are tomorrow's
        out = cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        self.assertTrue(out["no_session"])
        self.assertEqual(api.placed, [])

    def test_an_inverted_book_switches_to_simulation_and_warns(self):
        api = FakeAPI({1: 36, 2: 37, 3: 38}, inverted=True)
        for t in (T, T + dt.timedelta(minutes=5), T.replace(hour=16, minute=51)):  # the readings of the day pile up
            cycle(api, self.con, self.c, t)
        self.assertEqual(api.placed, [], "decides, but doesn't send")
        self.assertTrue(any("al revés" in n[0][0] for n in self.notes))

    def test_swapped_book_sides_read_the_bid_as_the_takers(self):
        book = [{"position": 1, "ask": {"rate": 59.9, "amount": 1}, "bid": {"rate": 24.9, "amount": 9}}]
        self.assertEqual(bot.book_levels(book, "percent"), ([(59.9, 1.0)], [(24.9, 9.0)]))
        self.assertEqual(bot.book_levels(book, "percent", swapped=True), ([(24.9, 9.0)], [(59.9, 1.0)]),
                         "a lender asking 59.9% at the open is not a taker paying it")
        c = bot.conf({"bot": {"book_sides": "swapped", "spike": {"min_days": 3}}})
        api = FakeAPI({1: 36, 2: 37, 3: 38}, inverted=True)  # what IOL showed: «takers» above lenders everywhere
        for t in (T, T + dt.timedelta(minutes=5), T.replace(hour=16, minute=51)):
            cycle(api, self.con, c, t)
        self.assertFalse(any("al revés" in n[0][0] for n in self.notes), "read the other way, the book is sane")
        self.assertEqual(len(api.placed), 1, "and the closing caución goes out")

    def test_the_closing_order_sweeps_the_takers_to_place_everything(self):
        api = FakeAPI({}, avail=120_000, levels={1: [(30, 50_000), (28, 100_000), (20, 1_000_000)]})
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        self.assertEqual((api.placed[0]["amount"], api.placed[0]["rate"]), (120_000, 28),
                         "the first taker only wants 50k: down to the second level to place it all")

    def test_after_the_close_pending_orders_are_cancelled_and_said(self):
        api = FakeAPI({1: 36})
        cycle(api, self.con, self.c, T.replace(hour=16, minute=57))
        bot.run(self.cfg, T.replace(hour=17, minute=3), live=True, api=api)
        self.assertEqual(api.cancelled, ["o1"])
        self.assertTrue(any("sin llenar" in n[0][0] for n in self.notes))
        self.assertIn("1 orden(es) sin llenar", self.con.execute("select summary from daily").fetchone()[0])

    def test_own_baseline_only_after_10_ordinary_days(self):
        c = bot.conf({})
        self.seed_a3(1, 21)
        for m in range(60):  # one long day of own readings at 40%
            self.con.execute("insert into rates (ts, day, hhmm, term_days, take_rate, bdays) values (?,?,?,?,?,?)",
                             (T.isoformat(), (T.date() - dt.timedelta(days=1)).isoformat(), "16:00", 1, 40, 1))
        self.con.commit()
        h = bot.history(self.con, 1, T, c)
        self.assertEqual((h["source"], h["median"]), ("a3", 21), "one day isn't a baseline")

    def test_the_rate_unit_is_fixed_once(self):
        api = FakeAPI({1: 36, 2: 37, 3: 38}, fraction=True)
        bot.snapshot(api, self.con, self.c, T)
        self.assertEqual(bot._state()["rate_unit"], "fraction")
        api.rates = {1: 350}  # a 350% spike sent as 3.5
        rows = bot.snapshot(api, self.con, self.c, T + dt.timedelta(minutes=5))
        self.assertAlmostEqual(rows[0]["take_rate"], 350.0)

    # -------------------------------------------------------------- failure handling
    def test_the_cycle_does_not_erase_what_other_steps_saved(self):
        self.seed_a3(1, 21)
        api = FakeAPI({1: 21}, mm_tna={"FONDOA": 21.0})
        cycle(api, self.con, self.c, T.replace(hour=10, minute=40))
        st = bot._state()
        self.assertIn("mm_yields", st)
        self.assertEqual(st.get("mm_checked"), T.date().isoformat())

    def test_an_unclear_error_is_retried_with_the_same_key_and_blocks_new_orders(self):
        api = FakeAPI({1: 36})
        api.fail_next = [iolapi.IOLError("sin conexión con IOL (TimeoutError)")]
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        d = self.con.execute("select id, status from decisions").fetchone()
        self.assertEqual(d["status"], "unknown")
        self.assertTrue(any("no respondió" in n[0][0] for n in self.notes))
        api.fail_next = [iolapi.IOLError("sin conexión con IOL (TimeoutError)")]
        out = cycle(api, self.con, self.c, T.replace(hour=16, minute=52))
        self.assertTrue(out.get("blocked"), "still unknown: nothing new is sent")
        self.assertEqual(api.placed, [])
        cycle(api, self.con, self.c, T.replace(hour=16, minute=53))
        self.assertEqual([p["idem"] for p in api.placed], [d["id"]], "resent with THE SAME key")
        self.assertEqual(self.con.execute("select status from decisions").fetchone()[0], "sent")

    def test_a_rejection_alerts_and_three_errors_stop_the_day(self):
        api = FakeAPI({1: 36})
        api.fail_next = [iolapi.IOLError("IOL POST /broker/repos → 400: x", 400) for _ in range(3)]
        for m in (51, 53, 55):
            cycle(api, self.con, self.c, T.replace(hour=16, minute=m))
        self.assertTrue(any("rechazó" in n[0][0] for n in self.notes))
        cycle(api, self.con, self.c, T.replace(hour=16, minute=57))
        self.assertTrue(any("Frené" in n[0][0] for n in self.notes))
        self.assertEqual(api.placed, [])

    def test_paused_never_touches_the_real_account(self):
        api = FakeAPI({1: 36})
        cycle(api, self.con, self.c, T.replace(hour=16, minute=50))
        bot.track(api, self.con, self.c, T.replace(hour=16, minute=55), dry=True)
        self.assertEqual(api.cancelled, [], "a paused bot doesn't cancel the order it had sent")

    def test_parked_pesos_after_the_rescue_cutoff_are_not_used_twice(self):
        self.seed_a3(1, 21)
        self.seed_a3(4, 22)
        api = FakeAPI({1: 21, 6: 30}, mm_tna={"FONDOA": 21.0})
        cycle(api, self.con, self.c, T.replace(hour=10, minute=40))
        self.assertEqual(len(api.subs), 1)
        api.rates[6] = 120  # a spike after the fund's cutoff: today's subscription can't be undone any more
        cycle(api, self.con, self.c, T.replace(hour=16, minute=10))
        self.assertEqual(api.placed, [], "the same pesos never go to the fund and to a caución")

    # -------------------------------------------------------------- the menu bar app's status
    def test_status_for_the_app(self):
        self.seed_a3(1, 21)
        api = FakeAPI({1: 30}, fill=True, avail=300_000, holdings={"FONDOA": 200_000})  # 30% < 36% threshold: no spike
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        track(api, self.con, self.c, T.replace(hour=16, minute=52))
        st = bot._state()
        st["mm_yields"] = {"day": T.date().isoformat(), "data": {"FONDOA": 20.0}}
        bot._save_state(st)
        self.assertTrue(status.refresh(api, self.con, self.c, T.replace(hour=16, minute=53), "abierto", force=True, dry=False))
        data = json.loads(status.path().read_text())
        self.assertEqual(oct(status.path().stat().st_mode & 0o777), "0o600")
        self.assertEqual(data["money"]["in_caucion"], 300_000)
        self.assertEqual(data["money"]["in_fund"], 200_000)
        self.assertEqual(data["money"]["total"], 500_000, "free (0 after placing) + caución + fund")
        self.assertEqual(len(data["cauciones"]), 1)
        self.assertAlmostEqual(data["yield"]["per_day"], 300_000 * 0.30 / 365 + 200_000 * 0.20 / 365, places=1)
        self.assertEqual(data["today"][0]["kind"], "Cierre")
        self.assertEqual(data["mode"], "en vivo")
        self.assertFalse(status.refresh(api, self.con, self.c, T.replace(hour=16, minute=55), "abierto"),
                         "at most every 5 minutes unless forced")

    def test_a_subscription_waiting_for_the_evening_still_counts(self):
        self.seed_a3(1, 21)
        api = FakeAPI({1: 21}, mm_tna={"FONDOA": 21.0}, avail=100_000)
        cycle(api, self.con, self.c, T.replace(hour=10, minute=50))
        self.assertEqual(len(api.subs), 1)
        data = status.build(self.con, self.c, T.replace(hour=11), {"available": 0, "committed": 100_000}, {}, {}, "abierto")
        self.assertEqual((data["money"]["total"], data["money"]["in_fund_pending"]), (100_000, 100_000),
                         "IOL shows it as committed and the fund isn't in the positions yet: still the user's money")

    # -------------------------------------------------------------- the job
    def test_run_throttles_and_respects_market_hours_and_pause(self):
        api = FakeAPI({1: 36})
        self.assertEqual(bot.run(self.cfg, T.replace(hour=9), live=True, api=api), 0)
        self.assertEqual(api.calls, [], "market closed")
        bot.run(self.cfg, T, live=True, api=api)
        bot.run(self.cfg, T + dt.timedelta(minutes=2), live=True, api=api)
        self.assertEqual(api.calls.count("terms"), 1, "every 5 min before 15:30; terms once a day")
        bot.set_paused(True)
        bot.run(self.cfg, T.replace(hour=16, minute=51), live=True, api=api)
        self.assertEqual(api.placed, [], "paused: looks and learns, doesn't place")
        bot.set_paused(False)

    def test_day_summary_and_learning_carry_no_amounts(self):
        api = FakeAPI({1: 36}, fill=True)
        cycle(api, self.con, self.c, T.replace(hour=16, minute=51))
        track(api, self.con, self.c, T.replace(hour=16, minute=52))  # IOL says it was filled
        text = bot.summarize_day(self.con, T.replace(hour=17, minute=30))
        self.assertIn("TNA promedio 36%", text)
        self.assertNotIn("$", text)
        self.assertIsNone(bot.summarize_day(self.con, T.replace(hour=17, minute=40)), "once a day")
        self.seed_history(1, 35, n=12)
        self.assertTrue(any("1 día hábil: mediana" in x for x in bot.learn(self.con, T)))

    # -------------------------------------------------------------- A3 history
    def test_a3_rows_count_business_days_on_the_market_calendar(self):
        def det(plazo, due, pp):
            return {"ticker": "CAARS", "plazo": f"{plazo:03d}", "fechaLiquidacion": due + "T00:00:00", "tasaPP": pp,
                    "maximo": pp * 2, "minimo": pp / 2, "ultimaTasa": pp, "montoConcertado": 1e9}
        days = [{"fecha": "2030-03-13", "details": [det(1, "2030-03-14", 26)]},
                {"fecha": "2030-03-14", "details": [det(6, "2030-03-20", 29.6), det(7, "2030-03-21", 30),
                                                    {"ticker": "CAUSD", "plazo": "006"}]},
                {"fecha": "2030-03-20", "details": [det(1, "2030-03-21", 22)]}]
        rows = {(r[0], r[1]): r for r in a3.rows(days)}
        self.assertEqual(rows[("2030-03-14", 6)][3], 1, "6 calendar days = 1 business day (the market was closed)")
        self.assertEqual(rows[("2030-03-14", 7)][3], 2)
        self.assertEqual(rows[("2030-03-13", 1)][3], 1)
        self.assertNotIn(("2030-03-14", "006"), rows, "only pesos")


if __name__ == "__main__":
    unittest.main()
