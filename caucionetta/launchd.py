"""A launchd LaunchAgent that runs `caucionetta bot once` every minute (the bot throttles itself).

`caucionetta install-agent` writes ~/Library/LaunchAgents/<LABEL>.plist; with --load it also bootstraps it. --live adds
the --live flag to the scheduled command, which STILL needs `live = true` in the config to send anything.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, config

LABEL = f"org.{APP_NAME}.bot"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def program() -> list[str]:
    exe = shutil.which(APP_NAME)
    return [exe] if exe else [sys.executable, "-m", APP_NAME]


def build(live: bool = False, interval: int = 60) -> dict:
    logs = config.state_dir()
    args = program() + ["bot", "once"] + (["--live"] if live else [])
    env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1"}
    for k in (f"{config.ENV_PREFIX}_CONFIG", f"{config.ENV_PREFIX}_STATE_DIR"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {"Label": LABEL, "ProgramArguments": args, "StartInterval": int(interval), "RunAtLoad": True,
            "ProcessType": "Background", "EnvironmentVariables": env,
            "StandardOutPath": str(logs / "launchd.log"), "StandardErrorPath": str(logs / "launchd.log")}


def install(live: bool = False, interval: int = 60, load: bool = False) -> Path:
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(plistlib.dumps(build(live, interval)))
    if load:
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(p)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], check=True)
    return p


def uninstall() -> bool:
    p = plist_path()
    if not p.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)
    p.unlink()
    return True
