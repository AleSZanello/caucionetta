"""Notifications behind one small function: `send(title, body, group=None)`.

Backends (config `notify.backend`):
  auto      macOS Notification Center on a Mac, the log elsewhere (default)
  macos     osascript `display notification`
  telegram  Bot API; token from $CAUCIONETTA_TELEGRAM_BOT_TOKEN (or Keychain account "telegram-token"),
            chat id from $CAUCIONETTA_TELEGRAM_CHAT_ID or `notify.telegram_chat_id`
  log       only the log
  none      nothing

The bot never puts amounts in a notification (rates and terms only): notifications end up on lock screens,
shared screens and third-party servers.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.parse
import urllib.request

from . import APP_NAME, credentials
from .config import ENV_PREFIX

log = logging.getLogger(APP_NAME)
_cfg: dict = {"backend": "auto", "telegram_chat_id": "", "service": APP_NAME}


def setup(cfg: dict) -> None:
    _cfg.update(cfg.get("notify") or {})
    _cfg["service"] = cfg["auth"]["keychain_service"]


def backend() -> str:
    b = _cfg.get("backend") or "auto"
    if b == "auto":
        return "macos" if sys.platform == "darwin" else "log"
    return b


def _osa(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos(title: str, body: str) -> None:
    script = f"display notification {_osa(body)} with title {_osa(APP_NAME)} subtitle {_osa(title)}"
    subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)


def _telegram(title: str, body: str) -> None:
    token = os.environ.get(f"{ENV_PREFIX}_TELEGRAM_BOT_TOKEN") or credentials.keychain_get(_cfg["service"], "telegram-token")
    chat = os.environ.get(f"{ENV_PREFIX}_TELEGRAM_CHAT_ID") or _cfg.get("telegram_chat_id")
    if not token or not chat:
        log.warning("telegram: missing bot token or chat id; notification only logged")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{body}"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=15) as r:
        json.load(r)


def send(title: str, body: str = "", group: str | None = None) -> None:
    """Fire and forget: a failing notifier never stops the bot. `group` is a stable key for this kind of
    message (kept for backends that can replace a previous notification)."""
    log.info("notify: %s — %s", title, body)
    b = backend()
    try:
        if b == "macos":
            _macos(title, body)
        elif b == "telegram":
            _telegram(title, body)
    except Exception as e:  # noqa: BLE001
        log.warning("notify (%s) failed: %s", b, type(e).__name__)
