#!/usr/bin/env python3
"""Renames the project: package dir, command, config/state dirs, Keychain service, launchd label, app names.

  python scripts/rename.py newname            # dry run: lists what would change
  python scripts/rename.py newname --apply

It replaces `caucionetta` / `CAUCIONETTA` / `Caucionetta` in tracked text files and moves the `caucionetta/` package directory and
files whose name contains `caucionetta`. Review the diff afterwards (git diff) — especially README links.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = "caucionetta"


def main() -> int:
    if len(sys.argv) < 2 or not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", sys.argv[1]):
        print(__doc__)
        return 2
    new, apply = sys.argv[1], "--apply" in sys.argv
    pairs = [(OLD, new), (OLD.upper(), new.upper()), (OLD.capitalize(), new.capitalize())]
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    for f in files:
        p = ROOT / f
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        out = text
        for a, b in pairs:
            out = out.replace(a, b)
        if out != text:
            print(f"edit   {f}")
            if apply:
                p.write_text(out)
    for f in sorted(files, key=len, reverse=True):
        name = Path(f).name
        if OLD in name.lower():
            renamed = name
            for a, b in pairs:
                renamed = renamed.replace(a, b)
            target = Path(f).with_name(renamed)
            print(f"move   {f} -> {target}")
            if apply:
                subprocess.run(["git", "mv", f, str(target)], cwd=ROOT, check=True)
    if (ROOT / OLD).is_dir():
        print(f"move   {OLD}/ -> {new}/")
        if apply:
            subprocess.run(["git", "mv", OLD, new], cwd=ROOT, check=True)
    if not apply:
        print("\n(dry run: add --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
