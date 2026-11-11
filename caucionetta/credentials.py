"""Credentials: environment variables first, then the macOS Keychain. Never a file, never a log line.

  CAUCIONETTA_USERNAME / CAUCIONETTA_PASSWORD      (any OS; e.g. from your password manager's CLI)
  Keychain: service "<keychain_service>", accounts "username" and "password"   (macOS)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .config import ENV_PREFIX

SECURITY = "/usr/bin/security"


class MissingCredentials(Exception):
    pass


def keychain_available() -> bool:
    return sys.platform == "darwin" and (os.path.exists(SECURITY) or bool(shutil.which("security")))


def keychain_get(service: str, account: str) -> str | None:
    if not keychain_available():
        return None
    try:
        r = subprocess.run([SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def keychain_set_username(service: str, username: str) -> None:
    subprocess.run([SECURITY, "add-generic-password", "-U", "-s", service, "-a", "username", "-w", username],
                   check=True, capture_output=True, timeout=10)


def keychain_set_password_interactive(service: str) -> bool:
    """`security` prompts for the password itself (twice) when -w is the last argument, so the password never
    appears in a process list or passes through this program."""
    r = subprocess.run([SECURITY, "add-generic-password", "-U", "-s", service, "-a", "password", "-w"])
    return r.returncode == 0


def keychain_delete(service: str) -> int:
    n = 0
    for acct in ("username", "password"):
        r = subprocess.run([SECURITY, "delete-generic-password", "-s", service, "-a", acct], capture_output=True)
        n += int(r.returncode == 0)
    return n


def get(cfg: dict) -> tuple[str, str]:
    service = cfg["auth"]["keychain_service"]
    user = os.environ.get(f"{ENV_PREFIX}_USERNAME") or keychain_get(service, "username")
    pw = os.environ.get(f"{ENV_PREFIX}_PASSWORD") or keychain_get(service, "password")
    if not user or not pw:
        raise MissingCredentials(f"no credentials: run `caucionetta auth` or set {ENV_PREFIX}_USERNAME / {ENV_PREFIX}_PASSWORD")
    return user, pw


def where(cfg: dict) -> dict:
    """Where each credential would come from (never its value)."""
    service = cfg["auth"]["keychain_service"]
    out = {}
    for acct in ("username", "password"):
        if os.environ.get(f"{ENV_PREFIX}_{acct.upper()}"):
            out[acct] = "env"
        elif keychain_get(service, acct):
            out[acct] = "keychain"
        else:
            out[acct] = None
    return out
