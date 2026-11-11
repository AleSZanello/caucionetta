"""Configuration (TOML), state paths and the live-mode gate.

  config: $CAUCIONETTA_CONFIG, else $XDG_CONFIG_HOME/caucionetta/config.toml (default ~/.config/caucionetta/config.toml)
  state:  $CAUCIONETTA_STATE_DIR, else $XDG_STATE_HOME/caucionetta (default ~/.local/state/caucionetta), mode 0700

The file is optional: every key has a default (see config.example.toml). Credentials never go in it.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import json
import os
import tempfile
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from . import APP_NAME

ENV_PREFIX = APP_NAME.upper()

# Every default is a neutral starting point, not a recommendation. Times are the market's local time.
DEFAULTS: dict = {
    "timezone": "America/Argentina/Buenos_Aires",
    # Double opt-in for real orders: `live = true` here AND `--live` on the command line (or `--allow-live`
    # for the MCP server). Anything less is a dry run that decides, logs and notifies but never sends.
    "live": False,
    "notify": {"backend": "auto", "telegram_chat_id": ""},
    "auth": {"keychain_service": APP_NAME},
    "api": {"max_calls_month": 24000, "timeout_s": 30},
    "mcp": {"confirm_ttl_s": 120},
    "bot": {
        "enabled": True,
        "currency": "ARS",
        "terms_days": [1, 7],              # calendar days; the 1-business-day term is always allowed
        "market": ["10:30", "17:00"],
        "poll_min": 5, "close_watch": ["15:30", "17:00"], "close_poll_min": 1, "close_max_bdays": 3,
        "ladder_enabled": True, "ladder_from": "14:00", "ladder": [[2.0, 30], [3.0, 60]],
        "final_at": "16:50", "final_at_high": "16:45", "final_at_friday": "16:25", "last_call": "16:58",
        "final_at_bridge": "16:55", "final_at_monthend": "16:15", "final_fill_timeout_min": 2,
        "post_close": ["17:00", "17:15"],
        # limits and the circuit breaker
        "max_orders_day": 25, "max_errors_day": 3,
        "max_order_amount": 0,             # 0 = no cap per order (the bot uses the free pesos minus keep_ars)
        "keep_ars": 0,                     # pesos the bot never touches
        "min_amount": 1000, "min_net_tna": 0.0,
        "idle_alert_ars": 0,               # 0 = never warn about idle pesos after the close
        "book_sides": "doc",               # "doc" (ask = taker) or "swapped"
        "lock_premium_pp_per_day": 1.0, "fill_timeout_min": 10, "stress_ratio": 1.25,
        "spike": {"ratio": 1.5, "min_pp": 15, "min_days": 10, "baseline_days": 10, "vs_1d_ratio": 1.3},
        # money market parking: off unless you list same-day-rescue funds yourself
        "mm": {"enabled": False, "funds": [], "decide_at": "10:35", "subscribe_by": "14:45", "rescue_by": "14:55",
               "margin_pp": 0.5, "fee_pp": 2.4, "window_days": 14, "min_amount": 1000, "tax_rate": 0.0},
    },
}


def _xdg(var: str, fallback: str) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else Path.home() / fallback


def demo() -> bool:
    """CAUCIONETTA_DEMO=1: fictional data, an isolated state dir, no credentials, no network. See demo.py."""
    return os.environ.get(f"{ENV_PREFIX}_DEMO", "") not in ("", "0", "false")


DEMO_OVERRIDES = {"notify": {"backend": "log"}, "bot": {"mm": {"funds": ["DEMOMM"]}}}


def config_path() -> Path:
    v = os.environ.get(f"{ENV_PREFIX}_CONFIG")
    return Path(v).expanduser() if v else _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME / "config.toml"


def state_dir() -> Path:
    v = os.environ.get(f"{ENV_PREFIX}_STATE_DIR")
    if v:
        d = Path(v).expanduser()
    elif demo():  # never the real state
        d = Path(tempfile.gettempdir()) / f"{APP_NAME}-demo"
    else:
        d = _xdg("XDG_STATE_HOME", ".local/state") / APP_NAME
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(Exception):
    pass


def load(path: Path | str | None = None) -> dict:
    p = Path(path).expanduser() if path else config_path()
    user: dict = {}
    if demo():  # the demo never reads your config
        cfg = merge(DEFAULTS, DEMO_OVERRIDES)
        cfg["_path"] = "(demo: built-in)"
        return cfg
    if p.exists():
        try:
            user = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: {e}") from None
    cfg = merge(DEFAULTS, user)
    cfg["_path"] = str(p)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    if not isinstance(cfg.get("live"), bool):
        raise ConfigError("`live` has to be true or false")
    b = cfg["bot"]
    lo, hi = b["terms_days"]
    if not (1 <= int(lo) <= int(hi)):
        raise ConfigError("bot.terms_days has to be [min, max] with 1 <= min <= max")
    for k in ("max_orders_day", "max_errors_day"):
        if int(b[k]) < 1:
            raise ConfigError(f"bot.{k} has to be at least 1")
    if b["book_sides"] not in ("doc", "swapped"):
        raise ConfigError('bot.book_sides has to be "doc" or "swapped"')
    try:
        ZoneInfo(cfg["timezone"])
    except Exception:  # noqa: BLE001
        raise ConfigError(f"unknown timezone: {cfg['timezone']}") from None


def live_allowed(cfg: dict, flag: bool) -> tuple[bool, str]:
    """Real orders need BOTH the config switch and the explicit flag of this invocation."""
    if not cfg.get("live"):
        return False, "dry-run: falta `live = true` en el config"
    if not flag:
        return False, "dry-run: este comando no se corrió con --live"
    return True, "en vivo"


# ---------------------------------------------------------------- time

def tz(cfg: dict | None = None) -> ZoneInfo:
    return ZoneInfo((cfg or DEFAULTS)["timezone"])


DEMO_TIME = "2030-03-12T16:51:00"


def now_local(cfg: dict | None = None) -> dt.datetime:
    if demo():  # a frozen, obviously fictional moment: a Tuesday, minutes after the final placement time
        return dt.datetime.fromisoformat(os.environ.get(f"{ENV_PREFIX}_DEMO_TIME") or DEMO_TIME).replace(tzinfo=tz(cfg))
    return dt.datetime.now(tz(cfg))


# ---------------------------------------------------------------- state files

def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """Write next to the target and rename over it: readers (the menu bar polls) never see half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(name: str) -> dict:
    try:
        return json.loads((state_dir() / name).read_text())
    except (OSError, ValueError):
        return {}


def write_json(name: str, data: dict) -> None:
    atomic_write(state_dir() / name, json.dumps(data, indent=1, ensure_ascii=False))


def read_state() -> dict:
    return read_json("state.json")


def write_state(st: dict) -> None:
    write_json("state.json", st)


def update_state(**kv) -> None:
    """Re-read and change only these keys, so a stale copy never erases what another step saved."""
    st = read_state()
    st.update(kv)
    write_state(st)


class Busy(Exception):
    pass


@contextlib.contextmanager
def lock(name: str):
    """A non-blocking process lock in the state dir (one bot pass at a time)."""
    p = state_dir() / f"{name}.lock"
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(name) from None
        yield
    finally:
        os.close(fd)
