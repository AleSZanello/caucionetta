#!/usr/bin/env python3
"""Writes docs/img/terminal.svg: a terminal window with the REAL output of a few demo-mode commands (fictional
data). Pure SVG text, no browser needed.

  python scripts/render_terminal.py
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = [["status"], ["bot", "once"]]
W, LH, PAD, TOP = 1220, 19, 22, 46
COLORS = {"prompt": "#5fd38d", "cmd": "#f2f2f2", "out": "#c9ccd1", "dim": "#8b9099", "accent": "#f0a35c"}


def run(state: Path) -> list[tuple[str, str]]:
    env = dict(os.environ, CAUCIONETTA_DEMO="1", CAUCIONETTA_STATE_DIR=str(state), PYTHONPATH=str(ROOT), COLUMNS="140")
    subprocess.run([sys.executable, "-m", "caucionetta", "demo"], env=env, check=True, capture_output=True)
    lines: list[tuple[str, str]] = []
    for args in COMMANDS:
        lines.append(("cmd", "caucionetta " + " ".join(args)))
        out = subprocess.run([sys.executable, "-m", "caucionetta", *args], env=env, capture_output=True, text=True).stdout
        for x in out.rstrip("\n").splitlines():
            kind = "accent" if ("SIMULADO" in x or "[DEMO" in x or x.lstrip().startswith("→")) else "out"
            lines.append((kind, x))
        lines.append(("out", ""))
    return lines[:-1]


def svg(lines: list[tuple[str, str]]) -> str:
    h = TOP + PAD + LH * len(lines)
    rows = []
    for i, (kind, text) in enumerate(lines):
        y = TOP + PAD + LH * i
        if kind == "cmd":
            rows.append(f'<text x="{PAD}" y="{y}"><tspan fill="{COLORS["prompt"]}">$ </tspan>'
                        f'<tspan fill="{COLORS["cmd"]}">{html.escape(text)}</tspan></text>')
        else:
            rows.append(f'<text x="{PAD}" y="{y}" fill="{COLORS[kind]}" xml:space="preserve">{html.escape(text)}</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" viewBox="0 0 {W} {h}" role="img"
 aria-label="caucionetta en la terminal, modo demo con datos ficticios">
<rect width="{W}" height="{h}" rx="10" fill="#1e2127"/>
<rect width="{W}" height="30" rx="10" fill="#2a2e35"/><rect y="20" width="{W}" height="10" fill="#2a2e35"/>
<circle cx="18" cy="15" r="6" fill="#ff5f57"/><circle cx="38" cy="15" r="6" fill="#febc2e"/><circle cx="58" cy="15" r="6" fill="#28c840"/>
<text x="{W // 2}" y="19" fill="{COLORS['dim']}" font-family="-apple-system,Segoe UI,sans-serif" font-size="12"
 text-anchor="middle">caucionetta — demo (datos ficticios)</text>
<g font-family="SFMono-Regular,Menlo,Consolas,monospace" font-size="13">
{chr(10).join(rows)}
</g></svg>
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = ROOT / "docs" / "img" / "terminal.svg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(svg(run(Path(tmp) / "state")))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
