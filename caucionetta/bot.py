"""A caución colocadora (repo lending) bot for IOL.

What it does, with the defaults in config.example.toml (every number is configurable):
- only caución colocadora in pesos, 1 to 7 calendar days (the 1-business-day term is always allowed: before a
  long bank closure it spans more calendar days). A caución can't be unwound before it's due;
- terms are compared by BUSINESS days to maturity: IOL lists one term per business day, so a term's position in
  the list is its business days;
- spikes first: a term paying far above its own baseline takes the free pesos at once;
- on high-probability days (bank closure of 4+ days, last business day of the month, mid-December to
  mid-January) an optional limit order rests in the book from `ladder_from` at a multiple of the baseline, and is
  cancelled at the final placement;
- at the final placement (`final_at`, earlier or later depending on the kind of day) the idle pesos go to the best
  short caución, sweeping the takers' levels;
- a stress streak (today's 1-business-day rate well over its baseline) keeps money rolling at 1 business day;
- the baseline per business-day term comes from the bot's own readings once there are enough ordinary days, and
  from A3's public history until then;
- optional money market parking: on quiet days, when a same-day-rescue fund YOU listed pays more than the
  caución net of fees, the idle pesos go there before its cutoff, and come back when a spike shows up.

Safety: DRY-RUN BY DEFAULT. Every function that could send or cancel an order defaults to `dry=True`; `run()`
only goes live with `live = true` in the config AND `live=True` from the caller (the CLI's --live). Limit orders
only, IOL's fee estimate before each order (skipped if fees eat the interest), idempotency keys recorded before
sending, a circuit breaker (`max_errors_day`, `max_orders_day`) that turns the rest of the day into a dry run,
and an unclear send (timeout, 5xx) blocks new orders until it's resolved with the SAME key.

Decisions are plain code, never a model. Everything is recorded in SQLite (<state>/bot.sqlite, 0600).
Notifications carry rates and terms, never amounts.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import sqlite3
import statistics
import uuid

from . import APP_NAME, a3, config, notify
from . import api as iolapi

log = logging.getLogger(APP_NAME)

SCHEMA = """
create table if not exists rates(ts text, day text, hhmm text, term_id text, term_days integer, cal_days integer,
  due_date text, take_rate real, take_amount real, offer_rate real, offer_amount real, unit_sample real,
  bdays integer, high integer);
create index if not exists rates_term_day on rates(term_days, day);
create index if not exists rates_bdays_day on rates(bdays, day);
create table if not exists decisions(id text primary key, ts text, day text, kind text, term_days integer,
  term_id text, cal_days integer, amount real, rate real, baseline real, fees real, net_tna real, reason text,
  status text, order_id text, dry integer, error text, bdays integer, req text);
create table if not exists orders(order_id text primary key, decision_id text, ts text, day text, term_days integer,
  cal_days integer, due_date text, amount real, rate real, status text, filled real, checked text, kind text);
create table if not exists notes(ts text, kind text, text text);
create table if not exists daily(day text primary key, summary text);
"""


def conf(cfg: dict | None = None) -> dict:
    """The bot section of the config, with defaults filled in (`cfg` may be a partial config)."""
    full = config.merge(config.DEFAULTS, {k: v for k, v in (cfg or {}).items() if not k.startswith("_")})
    return full["bot"]


def db() -> sqlite3.Connection:
    p = config.state_dir() / "bot.sqlite"
    new = not p.exists()
    con = sqlite3.connect(p)
    if new:
        os.chmod(p, 0o600)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA + a3.SCHEMA)
    con.commit()
    return con


def hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def within(t: dt.datetime, pair) -> bool:
    return hm(pair[0]) <= t.time() < hm(pair[1])


def paused() -> bool:
    return (config.state_dir() / "paused").exists()


def set_paused(on: bool) -> None:
    p = config.state_dir() / "paused"
    if on:
        p.touch()
    else:
        p.unlink(missing_ok=True)


_state = config.read_state
_save_state = config.write_state
_update_state = config.update_state


def heartbeat(ok: bool, summary: str) -> None:
    seq = int((_state().get("heartbeat") or {}).get("seq") or 0) + 1
    _update_state(heartbeat={"at": config.now_local().isoformat(timespec="seconds"), "ok": ok,
                             "summary": summary, "seq": seq})
    (log.info if ok else log.warning)("bot: %s", summary)


def fund_name(sym: str | None) -> str:
    return str(sym or "money market")


# ---------------------------------------------------------------- terms, flags and the book

def habiles(n) -> str:
    return "1 día hábil" if int(n or 0) == 1 else f"{n} días hábiles"


def cal_days(due: str | None, today: dt.date, fallback: int) -> int:
    try:
        d = dt.date.fromisoformat((due or "")[:10])
        return max(1, (d - today).days)
    except ValueError:
        return max(1, fallback)


def all_terms(api, c: dict, t: dt.datetime) -> list[dict]:
    """Every caución term of the session, sorted, with `bdays` = business days to maturity (its position: IOL
    lists one term per business day). Asked once a day."""
    st = _state()
    cache = st.get("terms") or {}
    if cache.get("day") != t.date().isoformat() or not cache.get("data"):
        cache = {"day": t.date().isoformat(), "data": api.terms(c["currency"])}
        st["terms"] = cache
        _save_state(st)
    terms = sorted((x for x in cache["data"] if int(x.get("term_days") or 0) > 0), key=lambda x: int(x["term_days"]))
    return [dict(x, bdays=i + 1) for i, x in enumerate(terms)]


def day_flags(terms: list[dict], t: dt.datetime) -> dict:
    """What kind of day it is, read from the terms themselves (no holiday calendar needed)."""
    if not terms:
        return {"gap": None, "session": None, "long_closure": False, "month_end": False, "season": False,
                "friday": False, "high": False}
    b1 = terms[0]
    gap = int(b1["term_days"])
    try:
        due = dt.date.fromisoformat(str(b1.get("due_date"))[:10])
        session = due - dt.timedelta(days=gap)
    except ValueError:
        due, session = t.date() + dt.timedelta(days=gap), t.date()
    f = {"gap": gap, "session": session, "long_closure": gap >= 4, "month_end": due.month != session.month,
         "season": (session.month == 12 and session.day >= 15) or (session.month == 1 and session.day <= 20),
         "friday": session.weekday() == 4}
    f["high"] = f["long_closure"] or f["month_end"] or f["season"]
    return f


def final_time(c: dict, flags: dict) -> str:
    """When the idle pesos are placed, by kind of day (a heuristic; tune it in the config)."""
    if flags.get("long_closure") and (flags.get("month_end") or flags.get("season")):
        return c["final_at_high"]
    if flags.get("long_closure"):
        return c["final_at_bridge"]
    if flags.get("month_end") or flags.get("season"):
        return c["final_at_monthend"]
    if flags.get("friday"):
        return c["final_at_friday"]
    return c["final_at"]


def book_levels(book: list[dict], unit: str | None, swapped: bool = False) -> tuple[list, list]:
    """(takers, lenders) as [(rate %, amount)], best first: the takers paying the most, the lenders asking least.
    Lending crosses with the takers. `swapped` (config `book_sides`): IOL's reference says `ask` is the taker; if
    your live book looks the other way round (every term «crossed»), read the sides swapped."""
    asks, bids = [], []
    for x in book or []:
        if not isinstance(x, dict):
            continue
        a, b = x.get("ask") or {}, x.get("bid") or {}
        if swapped:
            a, b = b, a
        ra, rb = iolapi.pct(a.get("rate"), unit), iolapi.pct(b.get("rate"), unit)
        if ra:
            asks.append((ra, float(a.get("amount") or 0)))
        if rb:
            bids.append((rb, float(b.get("amount") or 0)))
    return sorted(asks, key=lambda z: -z[0]), sorted(bids, key=lambda z: z[0])


def snapshot(api, con, c: dict, t: dt.datetime, max_bdays: int | None = None,
             terms: list[dict] | None = None, flags: dict | None = None) -> list[dict]:
    """Rates now for the allowed terms, stored. `take_rate` = what the best taker (tomador) pays: what lending
    gets. The rate unit is detected once and kept (state › rate_unit)."""
    lo, hi = c["terms_days"]
    terms = terms if terms is not None else all_terms(api, c, t)
    chosen = [x for x in terms if ((lo <= int(x["term_days"]) <= hi) or x["bdays"] == 1)
              and not (max_bdays and x["bdays"] > max_bdays)]
    books = {str(x["term_id"]): api.quotes(x["term_id"], c["currency"]) for x in chosen}
    st = _state()
    unit = st.get("rate_unit")
    if not unit:
        unit = iolapi.detect_unit([(lv.get(side) or {}).get("rate") for bk in books.values() for lv in bk or []
                                   if isinstance(lv, dict) for side in ("ask", "bid")])
        if unit:
            st["rate_unit"] = unit
            _save_state(st)
    rows = []
    high = int(bool((flags or {}).get("high")))
    for term in chosen:
        days, bd = int(term["term_days"]), term["bdays"]
        asks, bids = book_levels(books.get(str(term["term_id"])), unit, c.get("book_sides") == "swapped")
        r = {"term_id": term["term_id"], "term_days": days, "bdays": bd, "due_date": term.get("due_date"),
             # IOL's term_days are calendar days from the term's session: the interest days
             "cal_days": days or cal_days(term.get("due_date"), t.date(), days),
             "min_amount": float(term.get("min_amount") or 0), "max_amount": float(term.get("max_amount") or 0),
             "take_rate": asks[0][0] if asks else None, "take_amount": asks[0][1] if asks else 0.0,
             "offer_rate": bids[0][0] if bids else None, "offer_amount": bids[0][1] if bids else 0.0,
             "ask_levels": asks, "unit": unit or "percent", "unit_sample": None}
        rows.append(r)
        con.execute("insert into rates (ts, day, hhmm, term_id, term_days, cal_days, due_date, take_rate, take_amount,"
                    " offer_rate, offer_amount, unit_sample, bdays, high) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t.isoformat(timespec="seconds"), t.date().isoformat(), t.strftime("%H:%M"), str(r["term_id"]),
                     days, r["cal_days"], r["due_date"], r["take_rate"], r["take_amount"], r["offer_rate"],
                     r["offer_amount"], None, bd, high))
    con.commit()
    return rows


def book_inverted(rows: list[dict], t: dt.datetime) -> bool:
    """Sanity check of our reading of the book: a resting book has the takers BELOW the lenders (otherwise they'd
    have traded). If most readings today show takers above lenders, the sides are probably swapped."""
    st = _state()
    b = st.get("book") if (st.get("book") or {}).get("day") == t.date().isoformat() else {"day": t.date().isoformat(),
                                                                                           "both": 0, "crossed": 0}
    for r in rows:
        if r["take_rate"] and r["offer_rate"]:
            b["both"] += 1
            b["crossed"] += int(r["take_rate"] > r["offer_rate"])
    st["book"] = b
    _save_state(st)
    return b["both"] >= 5 and b["crossed"] / b["both"] > 0.5


# ---------------------------------------------------------------- baselines

def history(con, bdays: int, t: dt.datetime, c: dict) -> dict:
    """Baseline of a business-day term. Its own readings once there are `min_days` ordinary days of them (the
    median of each day's median, so frequent readings near the close don't weigh more, and without
    high-probability days); until then, the median of A3's daily weighted rate for the same business days."""
    sp = c["spike"]
    since = (t.date() - dt.timedelta(days=sp["baseline_days"] * 2 + 7)).isoformat()
    by_day: dict = {}
    for day, v in con.execute("select day, take_rate from rates where bdays=? and day<? and day>=? and "
                              "take_rate is not null and coalesce(high,0)=0", (bdays, t.date().isoformat(), since)):
        by_day.setdefault(day, []).append(v)
    days = sorted(by_day)[-int(sp["baseline_days"]):]
    if len(days) >= int(sp.get("min_days", 10)):
        meds = [statistics.median(by_day[d]) for d in days]
        allv = sorted(v for d in days for v in by_day[d])
        return {"source": "iol", "n": len(days), "median": statistics.median(meds),
                "p90": allv[min(len(allv) - 1, int(len(allv) * 0.9))]}
    a3v = [r[0] for r in con.execute("select pp from a3_daily where bdays=? and day<? and pp is not null "
                                     "order by day desc limit ?", (bdays, t.date().isoformat(), sp["baseline_days"]))]
    if len(a3v) >= 3:
        return {"source": "a3", "n": len(a3v), "median": statistics.median(a3v), "p90": None}
    return {"source": None, "n": 0, "median": None, "p90": None}


def threshold(h: dict, rate_1d: float | None, sp: dict, bdays: int) -> tuple[float | None, float | None]:
    """(spike threshold, baseline): far above its own baseline (and its p90), or without one, far above the
    1-business-day rate (only for longer terms)."""
    if h.get("median"):
        return max(h["median"] * sp["ratio"], h["median"] + sp["min_pp"], h.get("p90") or 0), h["median"]
    if bdays > 1 and rate_1d:
        return max(rate_1d * sp["vs_1d_ratio"], rate_1d + sp["min_pp"]), rate_1d
    return None, None


def stressed(con, t: dt.datetime, base_1d: float | None, c: dict) -> bool:
    """A stress streak: today's 1-business-day rate `stress_ratio` over its baseline. Keep rolling at 1 business
    day instead of locking a longer term at today's rate."""
    if not base_1d:
        return False
    vals = [r[0] for r in con.execute("select take_rate from rates where bdays=1 and day=? and take_rate is not null",
                                      (t.date().isoformat(),))]
    return bool(vals) and statistics.median(vals) >= base_1d * float(c["stress_ratio"])


# ---------------------------------------------------------------- money and orders

def available(api, c: dict, t: dt.datetime | None = None) -> float:
    b = api.balances() or {}
    ars = b.get("ars") or {}
    try:
        return max(0.0, float(((ars.get("available") or {}).get("t0")) or 0) - float(c["keep_ars"] or 0))
    except (TypeError, ValueError):
        return 0.0


def fees_of(est: list[dict]) -> float:
    total = 0.0
    for x in est or []:
        try:
            total += float(x.get("amount") or 0) + float(x.get("vat_amount") or 0)
        except (TypeError, ValueError):
            continue
    return total


def net_tna(amount: float, rate_pct: float, days: int, fees: float) -> float:
    interest = amount * rate_pct / 100 * days / 365
    return (interest - fees) / amount * 365 / days * 100 if amount > 0 and days > 0 else 0.0


def floor_amount(r: dict, c: dict) -> float:
    return max(float(c["min_amount"]), r.get("min_amount") or 0)


def sweep(levels: list, want: float, floor: float) -> tuple[float | None, float]:
    """Walks the takers from the best rate down to `floor`: (the lowest rate needed, how much they take). A
    level without an amount counts as enough."""
    total, limit = 0.0, None
    for rate, amt in levels:
        if rate < floor:
            break
        limit = rate
        if not amt:
            return limit, float("inf")
        total += amt
        if total >= want:
            break
    return limit, total


def size(avail: float, r: dict, c: dict, floor: float = 0.0) -> tuple[float, float | None]:
    """(amount, limit rate) to lend now at `floor` or more, across as many levels as needed, capped by the term's
    maximum and `max_order_amount`."""
    levels = r.get("ask_levels") or ([(r["take_rate"], r.get("take_amount") or 0)] if r.get("take_rate") else [])
    limit, depth = sweep(levels, avail, floor)
    if limit is None:
        return 0.0, None
    cap = [avail, depth]
    if r.get("max_amount"):
        cap.append(r["max_amount"])
    if float(c.get("max_order_amount") or 0) > 0:
        cap.append(float(c["max_order_amount"]))
    amt = min(cap)
    return (round(amt, 2), limit) if amt >= floor_amount(r, c) else (0.0, None)


def open_orders(con, t: dt.datetime, kind: str | None = None) -> list:
    q = "select * from orders where status in ('sent','pending') and day=?"
    args = [t.date().isoformat()]
    if kind:
        q += " and coalesce(kind,'take')=?"
        args.append(kind)
    return con.execute(q, args).fetchall()


def place(api, con, c: dict, t: dt.datetime, kind: str, r: dict, amount: float, rate_pct: float,
          baseline: float | None, reason: str, dry: bool = True, notify_: bool = True,
          shown: float | None = None) -> dict:
    """One limit order (recorded before it's sent: its id is the idempotency key, so a timeout never doubles it).
    kind: spike / final (take what the book pays now), rest (waits in the book) or manual (an approved proposal)."""
    did = str(uuid.uuid4())
    rate_api = iolapi.to_api(rate_pct, r.get("unit") or "percent")
    dec = {"id": did, "ts": t.isoformat(timespec="seconds"), "day": t.date().isoformat(), "kind": kind,
           "term_days": r["term_days"], "term_id": str(r["term_id"]), "cal_days": r["cal_days"], "amount": amount,
           "rate": rate_pct, "baseline": baseline, "fees": None, "net_tna": None, "reason": reason,
           "status": "planned", "order_id": None, "dry": int(dry), "error": None, "bdays": r["bdays"]}
    try:
        dec["fees"] = fees_of(api.estimate(r["term_id"], amount, rate_api, c["currency"]))
        dec["net_tna"] = round(net_tna(amount, rate_pct, r["cal_days"], dec["fees"]), 2)
    except iolapi.IOLError as e:
        dec["error"] = str(e)[:200]
    if dec["net_tna"] is not None and dec["net_tna"] <= float(c["min_net_tna"]):
        dec["status"], dec["error"] = "skipped", "la comisión se come el interés"
    elif dec["error"]:
        dec["status"] = "error"
    con.execute("insert into decisions (id, ts, day, kind, term_days, term_id, cal_days, amount, rate, baseline, fees,"
                " net_tna, reason, status, order_id, dry, error, bdays) values (:id,:ts,:day,:kind,:term_days,"
                ":term_id,:cal_days,:amount,:rate,:baseline,:fees,:net_tna,:reason,:status,:order_id,:dry,:error,:bdays)",
                dec)
    con.commit()
    if dec["status"] != "planned":
        log.info("decision %s: %s %s → %s (%s)", did[:8], kind, habiles(r["bdays"]), dec["status"], dec["error"])
        return dec
    if dry:
        dec["status"] = "dry"
        log.info("DRY-RUN would place caución %s: %s, %d calendar days, limit %.2f%% TNA, amount %.2f %s — %s",
                 did[:8], habiles(r["bdays"]), r["cal_days"], rate_pct, amount, c["currency"], reason)
    else:
        req = {"term_id": r["term_id"], "amount": amount, "rate_api": rate_api, "currency": c["currency"],
               "rate": rate_pct, "term_days": r["term_days"], "cal_days": r["cal_days"], "due_date": r["due_date"]}
        con.execute("update decisions set req=? where id=?", (json.dumps(req), did))
        try:
            dec["order_id"] = api.place(r["term_id"], amount, rate_api, did, c["currency"])
            dec["status"] = "sent"
            _order_row(con, dec["order_id"], did, dec["ts"], dec["day"], req, kind)
        except iolapi.IOLError as e:
            dec["status"], dec["error"] = ("unknown" if unclear(e) else "error"), str(e)[:200]
        log.info("LIVE caución %s: %s, limit %.2f%% TNA, amount %.2f → %s %s", did[:8], habiles(r["bdays"]),
                 rate_pct, amount, dec["status"], dec["error"] or dec["order_id"])
    con.execute("update decisions set status=?, order_id=?, error=? where id=?",
                (dec["status"], dec["order_id"], dec["error"], did))
    con.commit()
    if dec["status"] == "unknown":
        alert("IOL no respondió al mandar una caución", "Reintento con la misma clave (no se duplica) y no mando "
              "nada nuevo hasta saber qué pasó. Mirá `caucionetta bot log`.", f"unknown-{did[:8]}")
    elif dec["status"] == "error":
        alert("IOL rechazó una caución", "El motivo quedó en `caucionetta bot log`.", f"error-{did[:8]}")
    if notify_ and dec["status"] in ("sent", "dry"):
        plazo = habiles(r["bdays"]) + (f" ({r['cal_days']} corridos)" if r["cal_days"] != r["bdays"] else "")
        verb = "Espero en el libro" if kind == "rest" else "Caución"
        notify.send(f"{verb} a {plazo} al {(shown or rate_pct):.0f}% TNA" + (" (simulada)" if dry else ""),
                    f"{reason}." + (f" Límite {rate_pct:.1f}%." if shown and abs(shown - rate_pct) >= 0.5 else "")
                    + f" Neto de comisión: {net_tna(amount, shown or rate_pct, r['cal_days'], dec['fees'] or 0):.0f}% TNA.",
                    group=f"order-{did[:8]}")
    return dec


def unclear(e: iolapi.IOLError) -> bool:
    """A timeout, a dropped connection or a server error: the order may have reached IOL."""
    return e.status is None or e.status >= 500 or e.status == 429


def alert(title: str, sub: str, key: str) -> None:
    """An alert without amounts."""
    notify.send(title, sub, group=f"alert-{key}")


def cancel(api, con, o, t: dt.datetime, dry: bool = True) -> bool:
    if dry:  # paused or simulated: never touch the real account
        log.info("DRY-RUN would cancel order %s", o["order_id"])
        return False
    try:
        api.cancel(o["order_id"])
    except iolapi.IOLError as e:
        log.warning("cancel %s failed: %s", o["order_id"], e)
        return False
    con.execute("update orders set status='cancelled', checked=? where order_id=?",
                (t.isoformat(timespec="seconds"), o["order_id"]))
    con.commit()
    log.info("LIVE cancelled order %s", o["order_id"])
    return True


def _order_row(con, order_id: str, did: str, ts: str, day: str, req: dict, kind: str) -> None:
    con.execute("insert or replace into orders (order_id, decision_id, ts, day, term_days, cal_days, due_date,"
                " amount, rate, status, filled, checked, kind) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, did, ts, day, req["term_days"], req["cal_days"], req["due_date"], req["amount"],
                 req["rate"], "sent", 0.0, ts, kind))


def resolve_unknown(api, con, t: dt.datetime, dry: bool = True) -> int:
    """Decisions whose request may or may not have reached IOL are sent again with THE SAME idempotency key:
    if the first one got through, IOL doesn't create a second. Returns how many are still unknown."""
    left = 0
    for d in con.execute("select * from decisions where day=? and status='unknown'", (t.date().isoformat(),)).fetchall():
        if dry:
            left += 1
            continue
        req = json.loads(d["req"] or "{}")
        try:
            if d["kind"] == "mm_in":
                oid = api.fund_subscribe(d["term_id"], d["amount"], d["id"])
            elif d["kind"] == "mm_out":
                oid = api.fund_redeem_all(d["term_id"], d["id"])
            else:
                oid = api.place(req["term_id"], req["amount"], req["rate_api"], d["id"], req.get("currency", "ARS"))
                _order_row(con, oid, d["id"], d["ts"], d["day"], req, d["kind"])
            con.execute("update decisions set status='sent', order_id=?, error=NULL where id=?", (oid, d["id"]))
        except iolapi.IOLError as e:
            if unclear(e):
                left += 1
                continue
            con.execute("update decisions set status='error', error=? where id=?", (str(e)[:200], d["id"]))
            alert("IOL rechazó una orden reintentada", "El motivo quedó en `caucionetta bot log`.", f"error-{d['id'][:8]}")
    con.commit()
    return left


def track(api, con, c: dict, t: dt.datetime, dry: bool = True) -> int:
    """Updates today's sent orders from IOL. A «take» order still pending after `fill_timeout_min` is cancelled
    (its money comes back and the next poll decides again); «rest» orders wait until the final placement."""
    open_ = open_orders(con, t)
    if not open_:
        return 0
    try:
        by_id = {str(o.get("order_id")): o for o in api.operations(t.date())}
    except iolapi.IOLError:
        return 0
    n = 0
    for o in open_:
        live = by_id.get(str(o["order_id"]))
        status = (live or {}).get("status") or o["status"]
        filled = float(((live or {}).get("executed") or {}).get("amount") or 0)
        age = (t - dt.datetime.fromisoformat(o["ts"])).total_seconds() / 60
        timeout = float(c["final_fill_timeout_min"] if o["kind"] == "final" else c["fill_timeout_min"])
        if status in ("sent", "pending") and (o["kind"] or "take") != "rest" and age >= timeout:
            if cancel(api, con, o, t, dry):
                status = "cancelled"
        if status != o["status"] or filled != (o["filled"] or 0):
            con.execute("update orders set status=?, filled=?, checked=? where order_id=?",
                        (status, filled, t.isoformat(timespec="seconds"), o["order_id"]))
            n += 1
    con.commit()
    return n


# ---------------------------------------------------------------- money market parking (opt-in)

def mm_yields(api, con, c: dict, t: dt.datetime) -> dict:
    """TNA of each configured fund over the last `window_days`, from its daily share price (net of the fund's
    fees). Asked once a day."""
    st = _state()
    cache = st.get("mm_yields") or {}
    if cache.get("day") == t.date().isoformat():
        return cache.get("data") or {}
    out = {}
    w = int(c["mm"]["window_days"])
    for sym in c["mm"]["funds"]:
        try:
            bars = api.fund_bars(sym, t.date() - dt.timedelta(days=w + 7), t.date())
        except iolapi.IOLError:
            continue
        by_day = {}
        for b in sorted(bars or [], key=lambda b: str(b.get("datetime") or "")):
            try:
                by_day[str(b["datetime"])[:10]] = float(b["close"])
            except (KeyError, TypeError, ValueError):
                continue
        days = sorted(d for d, v in by_day.items() if v > 0)
        while len(days) >= 2 and by_day[days[-1]] == by_day[days[-2]]:
            days.pop()  # a price that didn't move (holidays, not published yet) is not a day of zero yield
        if len(days) < 2:
            continue
        end = dt.date.fromisoformat(days[-1])
        start = max((d for d in days if d <= (end - dt.timedelta(days=w)).isoformat()), default=days[0])
        n = (end - dt.date.fromisoformat(start)).days
        if n >= 3:
            out[sym] = round((by_day[days[-1]] / by_day[start] - 1) * 365 / n * 100, 2)
    st["mm_yields"] = {"day": t.date().isoformat(), "data": out}
    _save_state(st)
    for sym, tna in out.items():
        con.execute("insert into notes values (?,?,?)", (t.isoformat(timespec="seconds"), "mm_yield",
                                                         json.dumps({"symbol": sym, "tna": tna})))
    con.commit()
    return out


def mm_holdings(api, c: dict) -> dict:
    """{symbol: {qty, value}} of the configured funds that can be rescued now."""
    out = {}
    for pos in api.positions() or []:
        sym = ((pos.get("instrument") or {}).get("symbol")) or pos.get("symbol")
        if sym not in c["mm"]["funds"]:
            continue
        qty = float(((pos.get("availability") or {}).get("quantity_available")) or 0)
        price = float(((pos.get("market_value") or {}).get("current_price")) or 0)
        if qty > 0:
            out[sym] = {"qty": qty, "value": round(qty * price, 2)}
    return out


def _mm_decision(con, t: dt.datetime, kind: str, sym: str, amount: float, tna: float | None, reason: str,
                 dry: bool) -> str:
    did = str(uuid.uuid4())
    con.execute("insert into decisions (id, ts, day, kind, term_days, term_id, cal_days, amount, rate, baseline, fees,"
                " net_tna, reason, status, order_id, dry, error, bdays) values (?,?,?,?,0,?,0,?,?,NULL,0,?,?,?,NULL,?,NULL,0)",
                (did, t.isoformat(timespec="seconds"), t.date().isoformat(), kind, sym, amount, tna, tna, reason,
                 "planned", int(dry)))
    con.commit()
    return did


def _mm_done(con, did: str, status: str, order_id: str | None = None, error: str | None = None) -> None:
    con.execute("update decisions set status=?, order_id=?, error=? where id=?", (status, order_id, error, did))
    con.commit()


def mm_park(api, con, c: dict, t: dt.datetime, sym: str, amount: float, tna: float, reason: str,
            dry: bool = True) -> str:
    did = _mm_decision(con, t, "mm_in", sym, amount, tna, reason, dry)
    if dry:
        _mm_done(con, did, "dry")
        log.info("DRY-RUN would subscribe %.2f to %s — %s", amount, sym, reason)
        status = "dry"
    else:
        try:
            v = api.fund_validate("subscriptions", sym, amount) or {}
            if v.get("valid") is False:
                raise iolapi.IOLError("; ".join(str(m.get("message")) for m in v.get("messages") or [])[:160] or "no válida")
            _mm_done(con, did, "sent", api.fund_subscribe(sym, amount, did))
            status = "sent"
            log.info("LIVE subscribed %.2f to %s", amount, sym)
        except iolapi.IOLError as e:
            _mm_done(con, did, "unknown" if unclear(e) else "error", error=str(e)[:200])
            alert("No pude estacionar en el money market", "Lo reintento con la misma clave si IOL no respondió; "
                  "el motivo quedó en `caucionetta bot log`.", f"mm-{did[:8]}")
            return "error"
    notify.send(f"Estaciono los pesos en {fund_name(sym)} ({tna:.0f}% TNA)" + (" (simulado)" if dry else ""),
                reason, group=f"mm-{t.date().isoformat()}")
    return status


def mm_out(api, con, c: dict, t: dt.datetime, holdings: dict, reason: str, dry: bool = True) -> int:
    n = 0
    for sym, h in holdings.items():
        did = _mm_decision(con, t, "mm_out", sym, h["value"], None, reason, dry)
        if dry:
            _mm_done(con, did, "dry")
            log.info("DRY-RUN would redeem all of %s — %s", sym, reason)
            n += 1
            continue
        try:
            v = api.fund_validate("redemptions", sym) or {}
            if v.get("valid") is False:  # e.g. past the fund's cutoff: it would run tomorrow
                raise iolapi.IOLError("; ".join(str(m.get("message")) for m in v.get("messages") or [])[:160] or "no válido")
            _mm_done(con, did, "sent", api.fund_redeem_all(sym, did))
            log.info("LIVE redeemed all of %s", sym)
            n += 1
        except iolapi.IOLError as e:
            _mm_done(con, did, "unknown" if unclear(e) else "error", error=str(e)[:200])
            alert("No pude rescatar el money market", "El motivo quedó en `caucionetta bot log`.", f"mmout-{did[:8]}")
    if n:
        _update_state(mm_out_at=t.isoformat(timespec="seconds"))
        notify.send("Rescato el money market" + (" (simulado)" if dry else ""), reason,
                    group=f"mmout-{t.date().isoformat()}")
    return n


def parked_today(con, t: dt.datetime) -> float:
    """Pesos subscribed today whose order hasn't run yet: the balance may still show them, so they are never used
    twice."""
    return float(con.execute("select coalesce(sum(amount),0) from decisions where day=? and kind='mm_in' and "
                             "status in ('sent','dry')", (t.date().isoformat(),)).fetchone()[0])


def mm_unpark_today(api, con, t: dt.datetime, dry: bool = True) -> int:
    """Cancels today's pending subscriptions (to take a spike): until they run they can't be redeemed, but they
    can be cancelled."""
    n = 0
    for d in con.execute("select id, order_id, status from decisions where day=? and kind='mm_in' and status in ('sent','dry')",
                         (t.date().isoformat(),)).fetchall():
        if dry:  # paused or simulated: never touch the real account
            break
        try:
            if d["status"] == "sent":
                api.fund_cancel(d["order_id"])
            con.execute("update decisions set status='cancelled' where id=?", (d["id"],))
            n += 1
        except iolapi.IOLError:
            continue
    con.commit()
    return n


def mm_recently_out(t: dt.datetime, minutes: int = 30) -> bool:
    at = _state().get("mm_out_at")
    return bool(at) and (t - dt.datetime.fromisoformat(at)).total_seconds() < minutes * 60


# ---------------------------------------------------------------- the cycle

def cycle(api, con, c: dict, t: dt.datetime, dry: bool = True) -> dict:
    """One look at the market: store rates, follow orders, take spikes, rest an order on high-probability days,
    and at the final placement put the idle pesos to work."""
    terms = all_terms(api, c, t)
    flags = day_flags(terms, t)
    out = {"terms": 0, "spikes": 0, "placed": 0, "missed": 0, "flags": flags, "no_session": False, "dry": dry}
    if flags.get("session") and flags["session"] != t.date():
        out["no_session"] = True  # the terms are for another day: no trading today (a weekday holiday)
        return out
    closing = within(t, c["close_watch"])
    rows = snapshot(api, con, c, t, max_bdays=c["close_max_bdays"] if closing else None, terms=terms, flags=flags)
    track(api, con, c, t, dry)
    out["terms"] = len(rows)
    if resolve_unknown(api, con, t, dry):  # an order may be in IOL without us knowing: nothing new until it's clear
        out["blocked"] = True
        return out
    if not rows:
        return out
    today = t.date().isoformat()
    errors = con.execute("select count(*) from decisions where day=? and status in ('error','unknown')", (today,)).fetchone()[0]
    sent = con.execute("select count(*) from decisions where day=? and status='sent'", (today,)).fetchone()[0]
    if not dry and (errors >= int(c["max_errors_day"]) or sent >= int(c["max_orders_day"])):
        dry = True  # circuit breaker: something is off, stop sending for today
        out["breaker"] = True
        if _state().get("breaker") != today:
            _update_state(breaker=today)
            alert("Frené el bot por hoy (circuit breaker)", f"{errors} error(es) y {sent} orden(es) hoy alcanzan el "
                  "límite de seguridad. Mañana sigue solo; mirá `caucionetta bot log`.", f"breaker-{today}")
    if not dry and book_inverted(rows, t):  # our reading of the book looks swapped: decide, but don't send
        dry = True
        if _state().get("inverted_warned") != today:
            _update_state(inverted_warned=today)
            notify.send("El libro de caución se ve al revés", "La punta del tomador está por encima de la del "
                        "colocador en la mayoría de las lecturas: hoy el bot decide pero no manda órdenes. Revisá "
                        "`caucionetta cauciones` y la opción bot.book_sides.", group="inverted")
    out["dry"] = dry
    by_bd = {r["bdays"]: r for r in rows}
    r1 = by_bd.get(1)
    rate_1d = (r1 or {}).get("take_rate")
    sp = c["spike"]
    st = _state()
    seen = st.setdefault("spikes_seen", {})
    base_1d = history(con, 1, t, c).get("median")
    avail = None

    def free() -> float:
        nonlocal avail
        if avail is None:
            avail = available(api, c, t)
        return avail

    mm = c["mm"]
    mm_on = bool(mm["enabled"] and mm["funds"])
    holdings = None

    def held() -> dict:
        nonlocal holdings
        if holdings is None:
            try:
                holdings = mm_holdings(api, c)
            except iolapi.IOLError:
                holdings = {}
        return holdings

    yields = mm_yields(api, con, c, t) if mm_on else {}
    best_mm = max(yields.items(), key=lambda kv: kv[1]) if yields else None
    # what the caución is paying: its baseline, or today's level if higher
    today_1d = [r[0] for r in con.execute("select take_rate from rates where bdays=1 and day=? and take_rate is not null",
                                          (today,))]
    exp_cau = max([v for v in (base_1d, statistics.median(today_1d) if today_1d else None) if v], default=rate_1d)
    # the fund vs the caución net of fees and of your `tax_rate` (set it to your own situation)
    cau_net = (exp_cau - float(mm["fee_pp"])) * (1 - float(mm.get("tax_rate") or 0)) if exp_cau else None
    park_ok = bool(best_mm and cau_net is not None and not flags["high"]
                   and best_mm[1] > cau_net + float(mm["margin_pp"]))
    can_rescue = hm(mm["decide_at"]) <= t.time() < hm(mm["rescue_by"])
    # 0) money market out: a high-probability day (pesos for the resting orders), or the fund no longer pays more
    if mm_on and can_rescue and _state().get("mm_checked") != today and not mm_recently_out(t):
        _update_state(mm_checked=today)
        if held() and not park_ok:
            why = "día de picos probables: los pesos esperan la caución" if flags["high"] else \
                  "la caución neta paga más que el money market"
            if mm_out(api, con, c, t, held(), why, dry):
                avail, holdings = None, {}

    # 1) spikes: a term paying far above its own baseline takes the free pesos
    for r in rows:
        if not r["take_rate"]:
            continue
        thr, base = threshold(history(con, r["bdays"], t, c), rate_1d, sp, r["bdays"])
        if thr is None or r["take_rate"] < thr:
            continue
        out["spikes"] += 1
        reason = f"pico: {habiles(r['bdays'])} paga {r['take_rate']:.0f}% contra {base:.0f}% de referencia"
        if free() - parked_today(con, t) < floor_amount(r, c) and mm_on and can_rescue:
            if parked_today(con, t) and mm_unpark_today(api, con, t, dry):
                avail = None  # today's subscription hadn't run yet: cancelled, the pesos are free
            if held() and not mm_recently_out(t) and \
                    mm_out(api, con, c, t, held(), f"para tomar el pico de {r['take_rate']:.0f}% a {habiles(r['bdays'])}", dry):
                avail, holdings = None, {}  # same-day rescue: the pesos are back at once
        if free() < floor_amount(r, c):
            resting = open_orders(con, t, "rest")
            if resting and max(o["rate"] for o in resting) < r["take_rate"] - 5:
                for o in resting:  # the spike pays more than what's resting: free that money for it
                    cancel(api, con, o, t, dry)
                avail = None
        amt, _lim = size(free() - parked_today(con, t), r, c, floor=thr)  # never the pesos already in the fund
        if amt:  # limit at the threshold: it trades at the takers' rates (>= threshold), more of it fills
            d = place(api, con, c, t, "spike", r, amt, math.floor(thr * 10) / 10, base, reason, dry,
                      shown=r["take_rate"])
            if d["status"] in ("sent", "dry"):
                avail -= amt
                out["placed"] += 1
        else:
            key = f"{today}:{r['bdays']}"
            if key not in seen:  # learning: what it would have taken with free pesos
                seen[key] = r["take_rate"]
                con.execute("insert into notes values (?,?,?)", (t.isoformat(timespec="seconds"), "missed_spike",
                            json.dumps({"bdays": r["bdays"], "term_days": r["term_days"], "rate": r["take_rate"],
                                        "baseline": base})))
                con.commit()
                out["missed"] += 1
                notify.send(f"Pico de caución a {habiles(r['bdays'])}: {r['take_rate']:.0f}% TNA",
                            f"Su referencia es {base:.0f}%. No había pesos libres: lo anoto para aprender.",
                            group=f"spike-{key}")
    final = final_time(c, flags)
    # 2) high-probability day: an order waits in the book for a spike (only fills if a taker pays that much)
    if c.get("ladder_enabled", True) and flags["high"] and r1 and base_1d \
            and hm(c["ladder_from"]) <= t.time() < hm(final) \
            and not con.execute("select 1 from decisions where day=? and kind='rest' and status in ('sent','dry')",
                                (today,)).fetchone():
        steps = [tuple(x) for x in c["ladder"]]
        n = min(len(steps), int(free() // max(floor_amount(r1, c), 1)))
        chosen = steps[:n]
        why = ("cierre bancario de %d días" % flags["gap"]) if flags["long_closure"] else \
              ("fin de mes" if flags["month_end"] else "temporada de fin de año")
        for i, (mult, pp) in enumerate(chosen if n else []):
            amt = round(free() / (len(chosen) - i), 2)
            if float(c.get("max_order_amount") or 0) > 0:
                amt = min(amt, float(c["max_order_amount"]))
            if amt < floor_amount(r1, c):
                break
            rate = round(max(base_1d * mult, base_1d + pp), 1)
            d = place(api, con, c, t, "rest", r1, amt, rate, base_1d,
                      f"día de picos probables ({why}): espero un tomador que pague {mult:g}× la referencia", dry)
            if d["status"] in ("sent", "dry"):
                avail -= amt
                out["placed"] += 1
    # 2b) quiet day and the fund pays more than the caución net: park the idle pesos before the fund's cutoff
    if mm_on and park_ok and hm(mm["decide_at"]) <= t.time() < hm(mm["subscribe_by"]) \
            and not open_orders(con, t) and not mm_recently_out(t, 60):
        amt = round(free() - parked_today(con, t), 2)  # if the balance doesn't show the pending one yet, never twice
        if amt >= float(mm["min_amount"]):
            why = (f"día tranquilo: {fund_name(best_mm[0])} rinde {best_mm[1]:.1f}% contra unos "
                   f"{cau_net:.1f}% de la caución neta; rescate inmediato si aparece un pico")
            if mm_park(api, con, c, t, best_mm[0], amt, best_mm[1], why, dry) in ("sent", "dry"):
                avail = (avail or 0) - amt if avail is not None else None
                out["placed"] += 1
    # 3) final placement: cancel what's still resting and put every idle peso in the best short caución
    if hm(final) <= t.time() < hm(c["last_call"]):
        resting = open_orders(con, t, "rest")
        for o in resting:
            cancel(api, con, o, t, dry)
        if resting:
            avail = None  # the cancelled money comes back; the next poll sees it if IOL takes a moment
        idle = free() - parked_today(con, t)
        if idle >= float(c["min_amount"]):
            lock = float(c["lock_premium_pp_per_day"])
            stress = stressed(con, t, base_1d, c)
            cands = [r for r in rows if r["take_rate"] and size(idle, r, c)[0] and (not stress or r["bdays"] == 1)]
            if cands:
                best = max(cands, key=lambda r: r["take_rate"] - lock * (r["bdays"] - 1))
                why = "racha de tasas altas: sigo a 1 día hábil" if stress else \
                      f"pesos libres al cierre: el mejor plazo corto es {habiles(best['bdays'])}"
                amt, lim = size(idle, best, c)  # across the takers' levels until it's all placed
                d = place(api, con, c, t, "final", best, amt, lim, base_1d, why, dry)
                out["placed"] += int(d["status"] in ("sent", "dry"))
    _update_state(spikes_seen={k: v for k, v in seen.items() if k >= (t.date() - dt.timedelta(days=7)).isoformat()})
    return out


# ---------------------------------------------------------------- after the close

def post_close(api, con, c: dict, t: dt.datetime, dry: bool = True) -> int:
    """Once a day after the close: whatever is still pending is cancelled, unfilled orders and idle pesos are
    reported."""
    left = open_orders(con, t)
    track(api, con, c, t, dry)
    for o in open_orders(con, t):
        cancel(api, con, o, t, dry)
    try:
        idle = available(api, c) - parked_today(con, t)
    except iolapi.IOLError:
        idle = 0
    if not dry and float(c["idle_alert_ars"] or 0) > 0 and idle >= float(c["idle_alert_ars"]):
        alert("Quedaron pesos sin trabajar esta noche", "No se colocaron en caución ni en el money market. "
              "Mirá `caucionetta bot log` para saber por qué.", f"idle-{t.date().isoformat()}")
    done = {r["order_id"] for r in con.execute("select order_id from orders where day=? and (status='completed' or "
                                               "coalesce(filled,0)>0)", (t.date().isoformat(),))}
    takes = [o for o in left if (o["kind"] or "take") != "rest" and o["order_id"] not in done]
    if takes:
        notify.send("Cauciones sin llenar al cierre", f"{len(takes)} orden(es) no encontraron tomador: esos pesos "
                    "quedaron quietos hasta mañana. Mirá `caucionetta bot log`.", group=f"unfilled-{t.date().isoformat()}")
    return len(left)


def summarize_day(con, t: dt.datetime) -> str | None:
    """After the close, once a day: what was placed and at what rate (amounts stay in the database)."""
    day = t.date().isoformat()
    if con.execute("select 1 from daily where day=?", (day,)).fetchone():
        return None
    rows = con.execute("select kind, rate, coalesce(nullif(filled,0), amount) amount from orders "
                       "where day=? and (status='completed' or coalesce(filled,0)>0)", (day,)).fetchall()
    unfilled = con.execute("select count(*) from orders where day=? and kind!='rest' and status!='completed' "
                           "and coalesce(filled,0)=0", (day,)).fetchone()[0]
    dry = con.execute("select count(*) from decisions where day=? and status='dry'", (day,)).fetchone()[0]
    missed = con.execute("select count(*) from notes where kind='missed_spike' and ts like ?", (day + "%",)).fetchone()[0]
    if rows:
        w = sum(r["amount"] for r in rows) or 1
        avg = sum(r["rate"] * r["amount"] for r in rows) / w
        text = (f"{len(rows)} caución(es) ({sum(1 for r in rows if r['kind'] == 'spike')} por picos), "
                f"TNA promedio {avg:.0f}%")
    else:
        text = "Sin cauciones hoy" + (f" ({dry} simulada(s))" if dry else "")
    mm_rows = con.execute("select kind, net_tna from decisions where day=? and kind like 'mm_%' and status in ('sent','dry')",
                          (day,)).fetchall()
    if any(r["kind"] == "mm_in" for r in mm_rows):
        tna = next(r["net_tna"] for r in mm_rows if r["kind"] == "mm_in")
        text += f"; pesos estacionados en money market ({tna:.0f}% TNA)"
    if any(r["kind"] == "mm_out" for r in mm_rows):
        text += "; rescaté el money market"
    if unfilled:
        text += f"; {unfilled} orden(es) sin llenar"
    if missed:
        text += f"; {missed} pico(s) sin pesos libres"
    con.execute("insert into daily values (?,?)", (day, text))
    con.commit()
    notify.send("Cauciones de hoy", text, group=f"day-{day}")
    return text


def learn(con, t: dt.datetime, weeks: int = 4) -> list[str]:
    """What the data says (deterministic, no model): per business-day term, the usual rate and how often it
    spikes; the best moment of the day; spikes missed for lack of free pesos; A3's longer history."""
    since = (t.date() - dt.timedelta(weeks=weeks)).isoformat()
    out = []
    for (bd,) in con.execute("select distinct bdays from rates where day>=? and bdays is not null order by bdays",
                             (since,)).fetchall():
        vals = sorted(r[0] for r in con.execute("select take_rate from rates where bdays=? and day>=? "
                                                "and take_rate is not null", (bd, since)))
        if len(vals) < 10:
            continue
        med = statistics.median(vals)
        hot = sum(1 for v in vals if v >= med * 1.5)
        out.append(f"{habiles(bd)}: mediana {med:.0f}% TNA, picos (≥1,5× la mediana) en {hot} de {len(vals)} lecturas")
    by_hour = {hh: v for hh, v in con.execute("select substr(hhmm,1,2), avg(take_rate) from rates where bdays=1 and "
                                              "day>=? and take_rate is not null group by 1", (since,))}
    if len(by_hour) >= 3:
        best = max(by_hour, key=by_hour.get)
        out.append(f"La caución a 1 día hábil paga más, en promedio, a las {best} h ({by_hour[best]:.0f}% TNA)")
    missed = con.execute("select count(*) from notes where kind='missed_spike' and ts>=?", (since,)).fetchone()[0]
    if missed:
        out.append(f"{missed} pico(s) sin pesos libres en {weeks} semanas: dejar una parte libre los habría tomado")
    a3r = con.execute("select day, pp, mx, plazo from a3_daily where bdays=1 and day>=? and pp is not null order by day",
                      ((t.date() - dt.timedelta(days=120)).isoformat(),)).fetchall()
    if len(a3r) >= 20:
        base = statistics.median(r["pp"] for r in a3r)
        spikes = [r for r in a3r if r["mx"] and r["mx"] >= 2 * base]
        long_ = [r for r in a3r if r["plazo"] >= 4]
        long_spk = [r for r in long_ if r["mx"] and r["mx"] >= 2 * base]
        out.append(f"A3, 120 días: 1 día hábil con mediana {base:.0f}%; el máximo duplicó la mediana en {len(spikes)} "
                   f"de {len(a3r)} días" + (f" ({len(long_spk)} de {len(long_)} antes de cierres de 4+ días)" if long_ else ""))
    for line in out:
        con.execute("insert into notes values (?,?,?)", (t.isoformat(timespec="seconds"), "learn", line))
    con.commit()
    return out


# ---------------------------------------------------------------- one pass (what launchd / `bot run` call)

def is_live(cfg: dict | None, live: bool) -> bool:
    return config.live_allowed(cfg or config.DEFAULTS, live)[0] and not paused()


def run(cfg: dict | None = None, t: dt.datetime | None = None, live: bool = False, force: bool = False,
        api=None) -> int:
    """One pass. Meant to be called every minute: it throttles itself (every `poll_min`, every `close_poll_min`
    during `close_watch`). Dry-run unless `live = true` in the config AND live=True here, and not paused."""
    full = config.merge(config.DEFAULTS, {k: v for k, v in (cfg or {}).items() if not k.startswith("_")})
    c = full["bot"]
    t = t or config.now_local(full)
    if not c["enabled"]:
        heartbeat(True, "apagado (bot.enabled = false)")
        return 0
    dry = not is_live(full, live)
    con = db()
    try:
        return _run(full, c, con, t, dry, force, api)
    finally:
        con.close()


def _client(full: dict, api):
    return api or iolapi.make_client(full)


def _run(full: dict, c: dict, con, t: dt.datetime, dry: bool, force: bool, api) -> int:
    from . import status  # the menu bar's file; never stops the bot
    st = _state()
    open_market = t.weekday() < 5 and within(t, c["market"])
    if not open_market and not force:
        after = t.weekday() < 5 and t.time() >= hm(c["market"][1])
        if after and within(t, c["post_close"]) and st.get("post_close") != t.date().isoformat():
            api = _client(full, api)
            try:
                post_close(api, con, c, t, dry)
                _update_state(post_close=t.date().isoformat())
            except iolapi.IOLError as e:
                log.warning("post-close: %s", str(e)[:120])
            if con.execute("select 1 from decisions where day=? limit 1", (t.date().isoformat(),)).fetchone():
                summarize_day(con, t)
            try:  # the day's last picture for the menu bar
                status.refresh(api, con, c, t, "cerrado", force=True, dry=dry)
            except Exception as e:  # noqa: BLE001
                log.warning("status file: %s: %s", type(e).__name__, str(e)[:120])
        if after and st.get("a3_day") != t.date().isoformat():  # A3's public history, once a day after the close
            try:
                a3.update(con, t.date())
                _update_state(a3_day=t.date().isoformat())
            except Exception as e:  # noqa: BLE001 — history is a nice-to-have
                log.warning("A3: %s: %s", type(e).__name__, str(e)[:120])
        if after and con.execute("select 1 from decisions where day=? limit 1", (t.date().isoformat(),)).fetchone():
            summarize_day(con, t)
        if t.weekday() == 0 and after and st.get("learned") != t.date().isoformat():
            learn(con, t)
            _update_state(learned=t.date().isoformat())
        heartbeat(True, "mercado cerrado")
        return 0
    every = c["close_poll_min"] if within(t, c["close_watch"]) else c["poll_min"]
    last = st.get("last_poll")
    if not force and last and (t - dt.datetime.fromisoformat(last)).total_seconds() < every * 60 - 20:
        return 0
    _update_state(last_poll=t.isoformat(timespec="seconds"))
    api = _client(full, api)
    try:
        out = cycle(api, con, c, t, dry=dry)
    except iolapi.IOLError as e:
        heartbeat(False, str(e)[:160])
        if e.code in ("no_credentials", "auth_failed") or e.status in (401, 403):
            if _state().get("auth_warned") != t.date().isoformat():
                _update_state(auth_warned=t.date().isoformat())
                alert("No pude entrar a IOL", "Corré `caucionetta auth` (no reintento con la contraseña hasta entonces, "
                      "para que IOL no bloquee el usuario).", "auth")
        return 1
    except Exception as e:  # noqa: BLE001 — anything else (a response with another shape…): say it, don't die silent
        heartbeat(False, type(e).__name__)
        log.exception("cycle failed")
        if _state().get("crash_warned") != t.date().isoformat():
            _update_state(crash_warned=t.date().isoformat())
            alert("El bot falló", f"{type(e).__name__}: mirá el log (`caucionetta doctor` dice dónde está).",
                  f"crash-{t.date().isoformat()}")
        return 1
    try:  # the menu bar's data: every 5 min, or now if the bot did something
        status.refresh(api, con, c, t, "sin rueda hoy" if out.get("no_session") else "abierto", out["flags"],
                       force=bool(out.get("placed")), dry=dry)
    except Exception as e:  # noqa: BLE001
        log.warning("status file: %s: %s", type(e).__name__, str(e)[:120])
    if out.get("no_session"):
        heartbeat(True, "sin rueda hoy (feriado)")
        return 0
    tag = " · día de picos probables" if out["flags"].get("high") else ""
    heartbeat(True, f"{out['terms']} plazos · {out['spikes']} pico(s) · {out['placed']} orden(es){tag}"
              + (" · simulado (dry-run)" if out.get("dry") else " · en vivo"))
    return 0
