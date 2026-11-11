"""Public caución history from A3 Mercados (ex MAE): per day and term, the volume-weighted rate (tasaPP), max,
min, last and amount of the peso caución (CAARS). The bot uses it as a baseline until its own readings are
enough, indexed by BUSINESS days to maturity: a 6-calendar-day term before a long bank closure is a
1-business-day caución, not a normal 6-day one.

  GET https://api.marketdata.mae.com.ar/api/mercado/titulo/historicocauciones?oTitulo={"fechaDesde":..,"fechaHasta":..}
  (up to ~180 days per request; public, no account needed). Not an IOL endpoint.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import urllib.parse
import urllib.request

from . import APP_NAME, __version__

URL = "https://api.marketdata.mae.com.ar/api/mercado/titulo/historicocauciones"
SCHEMA = """
create table if not exists a3_daily(day text, plazo integer, due text, bdays integer, pp real, mx real, mn real,
  last real, monto real, primary key(day, plazo));
create index if not exists a3_bdays on a3_daily(bdays, day);
"""


def fetch(start: dt.date, end: dt.date, timeout: int = 60) -> list[dict]:
    out = []
    d = start
    while d <= end:
        e = min(d + dt.timedelta(days=170), end)
        q = urllib.parse.quote(json.dumps({"fechaDesde": d.isoformat(), "fechaHasta": e.isoformat()}, separators=(",", ":")))
        req = urllib.request.Request(f"{URL}?oTitulo={q}", headers={"User-Agent": f"{APP_NAME}/{__version__}",
                                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out.extend(json.load(r) or [])
        d = e + dt.timedelta(days=1)
    return out


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def rows(days: list[dict]) -> list[tuple]:
    """(day, plazo, due, bdays, pp, mx, mn, last, monto) for the peso caución. Business days to maturity are
    counted on the market's own calendar: the days that have data are the trading days."""
    trading = sorted({str(x.get("fecha", ""))[:10] for x in days if x.get("fecha")})
    out = []
    for x in days:
        day = str(x.get("fecha", ""))[:10]
        for d in x.get("details") or []:
            if d.get("ticker") != "CAARS":
                continue
            due = str(d.get("fechaLiquidacion") or "")[:10]
            try:
                plazo = int(str(d.get("plazo") or "0"))
            except ValueError:
                continue
            if not due or plazo <= 0:
                continue
            bd = bisect.bisect_right(trading, due) - bisect.bisect_right(trading, day)
            if not trading or due > trading[-1]:  # beyond the known calendar: weekdays as an approximation
                start = dt.date.fromisoformat(trading[-1] if trading and trading[-1] > day else day)
                extra = sum(1 for i in range(1, (dt.date.fromisoformat(due) - start).days + 1)
                            if (start + dt.timedelta(days=i)).weekday() < 5)
                bd += extra
            out.append((day, plazo, due, max(1, bd), _f(d.get("tasaPP")), _f(d.get("maximo")), _f(d.get("minimo")),
                        _f(d.get("ultimaTasa")), _f(d.get("montoConcertado"))))
    return out


def update(con, today: dt.date, since: dt.date | None = None) -> int:
    con.executescript(SCHEMA)
    last = con.execute("select max(day) from a3_daily").fetchone()[0]
    start = since or (dt.date.fromisoformat(last) - dt.timedelta(days=10) if last else today - dt.timedelta(days=365))
    rs = rows(fetch(start, today))
    con.executemany("insert or replace into a3_daily values (?,?,?,?,?,?,?,?,?)", rs)
    con.commit()
    return len(rs)
