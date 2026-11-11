"""IOL API client with a CLOSED list of endpoints.

Only what a caución (repo lending) bot needs: caución terms and order book, the fee estimate, placing and
cancelling a caución colocadora, the peso balance, positions, the operations list and, when you enable it, the
daily price / subscribe / redeem of the money market funds YOU list in the config. Nothing that buys or sells
other assets, borrows (caución tomadora), transfers or withdraws money: any other request raises before it
touches the network (see `allowed()` and tests/test_safety.py).

Auth: OAuth2 password grant on /auth/token; the access token is short-lived and the refresh token long-lived.
Tokens are cached in <state>/token.json (0600). Username and password come from `credentials.get()`.
Nothing of this is logged or printed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import APP_NAME, __version__, credentials, notify
from .config import DEFAULTS, atomic_write, now_local, state_dir

BASE = "https://api.iolinversiones.com"
FUND_SYMBOL = re.compile(r"^[A-Z0-9]{2,12}$")
ALLOWED = [
    ("POST", re.compile(r"^/auth/token$")),
    ("GET", re.compile(r"^/health$")),
    ("GET", re.compile(r"^/broker/market-data/repos/(terms|quotes)$")),
    ("POST", re.compile(r"^/broker/repos/estimate$")),
    ("POST", re.compile(r"^/broker/repos$")),
    ("DELETE", re.compile(r"^/broker/repos/[\w\-]+$")),
    ("GET", re.compile(r"^/broker/accounts/AR/balances$")),
    ("GET", re.compile(r"^/broker/accounts/AR/positions$")),
    ("GET", re.compile(r"^/broker/accounts/activities/operations$")),
    # money market parking (bodies carry the symbol: checked against the configured fund list by the client)
    ("POST", re.compile(r"^/broker/mutual-funds/(subscriptions|redemptions)(/validate)?$")),
    ("DELETE", re.compile(r"^/broker/mutual-funds/[\w\-]+$")),
]


class IOLError(Exception):
    def __init__(self, msg: str, status: int | None = None, code: str | None = None):
        super().__init__(msg)
        self.status, self.code = status, code


def allowed(method: str, path: str, funds=()) -> bool:
    """The closed list. A fund's daily price is allowed only for the funds YOU configured (exact symbols)."""
    if any(m == method and rx.match(path) for m, rx in ALLOWED):
        return True
    return method == "GET" and any(path == f"/broker/market-data/{f}/bars" for f in funds if FUND_SYMBOL.match(f))


def one_line(s: object, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def _save_private(p: Path, data: dict) -> None:
    atomic_write(p, json.dumps(data), mode=0o600)


def _calls(month: str) -> dict:
    try:
        d = json.loads((state_dir() / "calls.json").read_text())
    except (OSError, ValueError):
        d = {}
    return d if d.get("month") == month else {"month": month, "n": 0}


def count_call(path: str, cap: int | None) -> None:
    """IOL bills API calls past a monthly free quota (check their current fee table). Blocking reads would leave
    the bot blind, so the cap only warns, once a month."""
    month = now_local().strftime("%Y-%m")
    d = _calls(month)
    d["n"] += 1
    if cap and d["n"] >= cap and not d.get("warned"):
        d["warned"] = True
        notify.send("IOL: llegaste al tope de llamadas del mes",
                    f"{d['n']} llamadas (tope configurado: api.max_calls_month). Revisá la tabla de comisiones de IOL; "
                    "el bot sigue andando.", group=f"cap-{month}")
    _save_private(state_dir() / "calls.json", d)


class Client:
    def __init__(self, cfg: dict | None = None, cap: int | None = None, timeout: int | None = None,
                 funds: list[str] | None = None):
        self.cfg = cfg or DEFAULTS
        api = self.cfg.get("api") or DEFAULTS["api"]
        self.cap = cap if cap is not None else api["max_calls_month"]
        self.timeout = timeout or api["timeout_s"]
        self.funds = [str(f).upper() for f in (funds if funds is not None else self.cfg["bot"]["mm"]["funds"])]
        self.tok_path = state_dir() / "token.json"

    # ---------------------------------------------------------------- transport
    def _http(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
              headers: dict | None = None, auth: bool = True):
        try:
            return self._http1(method, path, params, body, headers, auth)
        except IOLError as e:
            # IOL can revoke an access token before it expires: drop it and retry ONCE with a fresh one
            # (refresh token first; never a loop on the password).
            if not (auth and e.status == 401 and self.drop_access()):
                raise
            return self._http1(method, path, params, body, headers, auth)

    def drop_access(self) -> bool:
        try:
            t = json.loads(self.tok_path.read_text())
        except (OSError, ValueError):
            return False
        if not t.get("access_token"):
            return False
        t.pop("access_token", None)
        t.pop("access_until", None)
        _save_private(self.tok_path, t)
        return True

    def _http1(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
               headers: dict | None = None, auth: bool = True):
        if not allowed(method, path, self.funds):
            raise IOLError(f"endpoint no permitido: {method} {path}")
        count_call(path, self.cap)
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        # the CDN in front of the API rejects Python's default User-Agent
        h = {"Accept": "application/json", "Content-Type": "application/json",
             "User-Agent": f"{APP_NAME}/{__version__}", **(headers or {})}
        if auth:
            h["Authorization"] = f"Bearer {self.token()}"
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise IOLError(f"sin conexión con IOL ({type(e).__name__})") from None
        try:
            msg = json.loads(raw or b"null")
        except ValueError:
            msg = None
        err = (msg or {}).get("error") if isinstance(msg, dict) else None
        if status >= 400 or err:
            e = err if isinstance(err, dict) else {}
            raise IOLError(f"IOL {method} {path} → {status}: {one_line(str(e.get('message') or err or ''), 160)}",
                           status, str(e.get("code") or "") or None)
        return msg.get("data") if isinstance(msg, dict) and "data" in msg else msg

    # ---------------------------------------------------------------- auth
    def token(self) -> str:
        now = dt.datetime.now(dt.UTC)
        try:
            t = json.loads(self.tok_path.read_text())
        except (OSError, ValueError):
            t = {}
        if t.get("auth_failed"):  # the password was rejected: never retry it every minute (IOL could lock the user)
            raise IOLError("IOL rechazó el usuario o la contraseña: corré `caucionetta auth`", code="auth_failed")
        if t.get("access_token") and t.get("access_until", "") > (now + dt.timedelta(seconds=60)).isoformat():
            return t["access_token"]
        data = None
        if t.get("refresh_token") and t.get("refresh_until", "") > now.isoformat():
            try:
                data = self._http("POST", "/auth/token", body={"grant_type": "refresh_token",
                                                                "refresh_token": t["refresh_token"]}, auth=False)
            except IOLError:
                data = None
        if not data:
            try:
                user, pw = credentials.get(self.cfg)
            except credentials.MissingCredentials as e:
                raise IOLError(str(e), code="no_credentials") from None
            try:
                data = self._http("POST", "/auth/token", body={"grant_type": "password", "username": user,
                                                                "password": pw}, auth=False)
            except IOLError as e:
                if e.status in (400, 401, 403):
                    _save_private(self.tok_path, {"auth_failed": now.isoformat()})
                raise
            t["refresh_until"] = (now + dt.timedelta(days=29)).isoformat()
        exp = int((data or {}).get("expires_in") or 900)
        t.update(access_token=data["access_token"], access_until=(now + dt.timedelta(seconds=exp)).isoformat(),
                 refresh_token=data.get("refresh_token") or t.get("refresh_token"))
        _save_private(self.tok_path, t)
        return t["access_token"]

    def forget_tokens(self) -> None:
        self.tok_path.unlink(missing_ok=True)

    # ---------------------------------------------------------------- caución
    def terms(self, currency: str = "ARS") -> list[dict]:
        return self._http("GET", "/broker/market-data/repos/terms", {"currency": currency}) or []

    def quotes(self, term_id, currency: str = "ARS") -> list[dict]:
        return self._http("GET", "/broker/market-data/repos/quotes", {"term_id": term_id, "currency": currency}) or []

    def balances(self) -> dict:
        return self._http("GET", "/broker/accounts/AR/balances") or {}

    def estimate(self, term_id, amount: float, rate_api: float | None, currency: str = "ARS") -> list[dict]:
        return self._http("POST", "/broker/repos/estimate", body=_order(term_id, amount, rate_api, currency)) or []

    def place(self, term_id, amount: float, rate_api: float | None, idem: str, currency: str = "ARS") -> str:
        uuid.UUID(idem)  # the idempotency key is a UUID: a retry with the same key never doubles the order
        d = self._http("POST", "/broker/repos", body=_order(term_id, amount, rate_api, currency),
                       headers={"X-Idempotency-Key": idem})
        oid = (d or {}).get("order_id")
        if not oid:
            raise IOLError("IOL no devolvió order_id")
        return str(oid)

    def cancel(self, order_id: str) -> dict:
        return self._http("DELETE", f"/broker/repos/{urllib.parse.quote(str(order_id), safe='')}") or {}

    def positions(self) -> list[dict]:
        d = self._http("GET", "/broker/accounts/AR/positions", {"valuation_currency": "ARS"}) or {}
        return (d.get("positions") if isinstance(d, dict) else d) or []

    def operations(self, day: dt.date, transaction_type: str = "repo") -> list[dict]:
        d = day.isoformat()
        return self._http("GET", "/broker/accounts/activities/operations",
                          {"start_date": d, "end_date": d, "transaction_type": transaction_type}) or []

    # ---------------------------------------------------------------- money market (only the configured funds)
    def _fund(self, symbol: str) -> str:
        s = str(symbol or "").upper()
        if s not in self.funds or not FUND_SYMBOL.match(s):
            raise IOLError(f"fondo no permitido: {symbol} (agregalo a bot.mm.funds si es de rescate en el día)")
        return s

    def fund_bars(self, symbol: str, start: dt.date, end: dt.date) -> list[dict]:
        d = self._http("GET", f"/broker/market-data/{self._fund(symbol)}/bars",
                       {"market": "BCBA", "from": f"{start.isoformat()} 00:00:00", "to": f"{end.isoformat()} 23:59:59"}) or {}
        return (d.get("bars") if isinstance(d, dict) else d) or []

    def fund_validate(self, kind: str, symbol: str, amount: float | None = None) -> dict:
        if kind not in ("subscriptions", "redemptions"):
            raise IOLError(f"operación no permitida: {kind}")
        body = {"symbol": self._fund(symbol)}
        body.update({"amount": round(float(amount), 2)} if kind == "subscriptions" else {"redeem_all": True})
        return self._http("POST", f"/broker/mutual-funds/{kind}/validate", body=body) or {}

    def fund_subscribe(self, symbol: str, amount: float, idem: str) -> str:
        uuid.UUID(idem)
        d = self._http("POST", "/broker/mutual-funds/subscriptions", headers={"X-Idempotency-Key": idem},
                       body={"symbol": self._fund(symbol), "amount": round(float(amount), 2)}) or {}
        if not d.get("transaction_id") or d.get("successful") is False:
            raise IOLError("IOL no aceptó la suscripción: " + "; ".join(str(m.get("message")) for m in d.get("messages") or [])[:160])
        return str(d["transaction_id"])

    def fund_redeem_all(self, symbol: str, idem: str) -> str:
        uuid.UUID(idem)
        d = self._http("POST", "/broker/mutual-funds/redemptions", headers={"X-Idempotency-Key": idem},
                       body={"symbol": self._fund(symbol), "redeem_all": True}) or {}
        if not d.get("transaction_id") or d.get("successful") is False:
            raise IOLError("IOL no aceptó el rescate: " + "; ".join(str(m.get("message")) for m in d.get("messages") or [])[:160])
        return str(d["transaction_id"])

    def fund_cancel(self, transaction_id: str) -> dict:
        return self._http("DELETE", f"/broker/mutual-funds/{urllib.parse.quote(str(transaction_id), safe='')}") or {}


def make_client(cfg: dict | None = None):
    """The real client, or the fictional one in demo mode (CAUCIONETTA_DEMO=1)."""
    from .config import demo
    if demo():
        from .demo import DemoAPI
        return DemoAPI()
    return Client(cfg)


def _order(term_id, amount: float, rate_api: float | None, currency: str) -> dict:
    body = {"amount": round(float(amount), 2), "currency": currency, "term_id": term_id,
            "price_type": "limit" if rate_api else "market"}
    if rate_api:
        body["rate"] = rate_api
    return body


# ---------------------------------------------------------------- rate units
# The reference doesn't say whether a TNA comes as 45.5 or 0.455. The unit is detected ONCE, from the median of a
# whole book (cauciones practically never pay under 3% TNA, so a median up to 3 means fractions), and then kept:
# guessing value by value would read a 350% spike sent as 3.5 as 3.5%.

def detect_unit(raws) -> str | None:
    vals = []
    for x in raws or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if len(vals) < 3:
        return None
    vals.sort()
    return "fraction" if vals[len(vals) // 2] <= 3 else "percent"


def pct(x, unit: str | None = None) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if unit == "fraction":
        return v * 100
    if unit == "percent":
        return v
    return v * 100 if v <= 3 else v  # no unit known yet: a guess


def to_api(rate_pct: float, unit) -> float:
    """Percent → the API's unit ('fraction' / 'percent', or a raw sample from the book to guess it)."""
    if unit not in ("fraction", "percent"):
        try:
            unit = "fraction" if 0 < float(unit) <= 3 else "percent"
        except (TypeError, ValueError):
            unit = "percent"
    return round(rate_pct / 100, 6) if unit == "fraction" else round(rate_pct, 4)
