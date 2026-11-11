#!/usr/bin/env python3
"""Renders the README's SwiftBar screenshots from the plugin's REAL output in demo mode (fictional data).

It builds the demo, runs one dry-run bot pass, runs menubar/swiftbar/caucionetta.1m.py, parses the SwiftBar lines and
lays them out as an HTML page styled like a macOS menu bar and its dropdown (light and dark), then screenshots it
with a headless Chromium. Nothing touches your real menu bar, SwiftBar install or state.

  uv pip install playwright && playwright install chromium-headless-shell
  python scripts/render_swiftbar.py            # writes docs/img/swiftbar-{light,dark}.png
"""
from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "img"

ICONS = {  # tiny stand-ins for the SF Symbols SwiftBar would draw
    "chart.line.uptrend.xyaxis": '<path d="M2 12 L6 7 L9 9 L14 3" fill="none" stroke="currentColor" stroke-width="1.6"/>'
                                 '<path d="M2 14 H14" stroke="currentColor" stroke-width="1.2"/>',
    "lock.fill": '<rect x="4" y="7" width="8" height="7" rx="1.5" fill="currentColor"/>'
                 '<path d="M5.5 7 V5 a2.5 2.5 0 0 1 5 0 V7" fill="none" stroke="currentColor" stroke-width="1.4"/>',
    "parkingsign.circle": '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.3"/>'
                          '<text x="8" y="11.2" font-size="8" text-anchor="middle" fill="currentColor" font-weight="700">P</text>',
    "arrow.clockwise": '<path d="M12.5 8 a4.5 4.5 0 1 1 -1.4 -3.3" fill="none" stroke="currentColor" stroke-width="1.4"/>'
                       '<path d="M11.5 1.8 V5 H8.3" fill="none" stroke="currentColor" stroke-width="1.4"/>',
    "pause.fill": '<rect x="4" y="3" width="3" height="10" rx="0.8" fill="currentColor"/>'
                  '<rect x="9" y="3" width="3" height="10" rx="0.8" fill="currentColor"/>',
    "play.fill": '<path d="M5 3 L13 8 L5 13 Z" fill="currentColor"/>',
    "eye.slash": '<path d="M1.5 8 C4 3.5 12 3.5 14.5 8 C12 12.5 4 12.5 1.5 8 Z" fill="none" stroke="currentColor" stroke-width="1.3"/>'
                 '<circle cx="8" cy="8" r="2" fill="currentColor"/><path d="M2.5 13.5 L13.5 2.5" stroke="currentColor" stroke-width="1.3"/>',
    "eye": '<path d="M1.5 8 C4 3.5 12 3.5 14.5 8 C12 12.5 4 12.5 1.5 8 Z" fill="none" stroke="currentColor" stroke-width="1.3"/>'
           '<circle cx="8" cy="8" r="2" fill="currentColor"/>',
    "doc.text": '<path d="M4 1.8 H9.5 L12.5 4.8 V14.2 H4 Z" fill="none" stroke="currentColor" stroke-width="1.3"/>'
                '<path d="M6 8 H10.5 M6 10.5 H10.5" stroke="currentColor" stroke-width="1.1"/>',
}


def icon(name: str | None) -> str:
    body = ICONS.get(name or "")
    return f'<svg class="ic" viewBox="0 0 16 16">{body}</svg>' if body else '<span class="ic"></span>'


def parse(line: str) -> dict:
    if line.strip() == "---":
        return {"text": "---", "sub": False}
    text, _, params = line.partition(" | ")
    p = dict(re.findall(r"(\w+)=((?:\"[^\"]*\")|\S+)", params))
    sub = text.startswith("--")
    return {"text": text[2:] if sub else text, "sub": sub, **p}


def plugin_output(state: Path) -> list[str]:
    env = dict(os.environ, CAUCIONETTA_DEMO="1", CAUCIONETTA_STATE_DIR=str(state), PYTHONPATH=str(ROOT))
    for args in (["demo"], ["bot", "once"]):
        subprocess.run([sys.executable, "-m", "caucionetta", *args], env=env, check=True, capture_output=True)
    out = subprocess.run([sys.executable, str(ROOT / "menubar/swiftbar/caucionetta.1m.py")], env=env, check=True,
                         capture_output=True, text=True).stdout
    return out.splitlines()


def page(lines: list[str], dark: bool) -> str:
    items = [parse(x) for x in lines]
    title, rest = items[0], items[1:]
    if rest and rest[0]["text"] == "---":  # SwiftBar: the first separator ends the title, it isn't drawn
        rest = rest[1:]
    rows = []
    for i, it in enumerate(rest):
        if it["text"] == "---":
            rows.append('<div class="sep"></div>')
            continue
        if it["sub"]:
            continue  # submenus stay closed; their parent shows a chevron
        has_sub = i + 1 < len(rest) and rest[i + 1]["sub"]
        color = it.get("color", "")
        if color:
            light_c, _, dark_c = color.partition(",")
            color = dark_c if dark and dark_c else light_c
        style = []
        if color:
            style.append(f"color:{color}")
        if it.get("size"):
            style.append(f"font-size:{it['size']}px")
        mono = " mono" if it.get("font") == "Menlo" else ""
        action = " action" if "bash" in it else ""
        rows.append(f'<div class="row{mono}{action}" style="{";".join(style)}">{icon(it.get("sfimage"))}'
                    f'<span class="t">{html.escape(it["text"])}</span>{"<span class=chev>›</span>" if has_sub else ""}</div>')
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
:root {{ --bar:{'rgba(30,30,32,.92)' if dark else 'rgba(246,246,246,.92)'}; --fg:{'#f2f2f2' if dark else '#1d1d1f'};
  --menu:{'rgba(40,40,44,.97)' if dark else 'rgba(238,238,238,.97)'}; --sep:{'rgba(255,255,255,.12)' if dark else 'rgba(0,0,0,.1)'};
  --hl:{'rgba(255,255,255,.16)' if dark else 'rgba(0,0,0,.08)'}; --desk:{'#1b2433' if dark else '#c9d6e8'}; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; width:620px; height:740px; background:linear-gradient(160deg,var(--desk),{'#2c1f33' if dark else '#e8d9cf'});
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",sans-serif; color:var(--fg); }}
.bar {{ height:26px; background:var(--bar); display:flex; align-items:center; justify-content:flex-end; gap:16px;
  padding:0 14px; font-size:13px; backdrop-filter:blur(20px); }}
.bar .item {{ display:flex; align-items:center; gap:5px; padding:2px 7px; border-radius:5px; }}
.bar .item.on {{ background:var(--hl); }}
.bar .dim {{ opacity:.75 }}
.menu {{ position:absolute; top:30px; left:150px; width:400px; background:var(--menu); border-radius:10px; padding:5px;
  box-shadow:0 12px 40px rgba(0,0,0,.35), 0 0 0 .5px {'rgba(255,255,255,.18)' if dark else 'rgba(0,0,0,.18)'}; font-size:13px; }}
.row {{ display:flex; align-items:center; gap:7px; padding:3px 9px; border-radius:5px; line-height:17px; white-space:pre; }}
.row.mono .t {{ font-family:Menlo,monospace; }}
.row .t {{ flex:1; overflow:hidden; text-overflow:ellipsis; }}
.ic {{ width:14px; height:14px; flex:none; display:inline-block; }}
.chev {{ opacity:.6; font-size:15px; }}
.sep {{ height:1px; background:var(--sep); margin:5px 9px; }}
</style></head><body>
<div class="bar"><span class="dim">•••</span>
 <span class="item on">{icon(title.get('sfimage'))}<span>{html.escape(title['text'])}</span></span>
 <span class="dim">Wi-Fi</span><span>mar 12 mar 16:51</span></div>
<div class="menu">{''.join(rows)}</div>
</body></html>"""


def main() -> int:
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        lines = plugin_output(Path(tmp) / "state")
        shown = "\n".join(lines).replace(str(Path(tmp) / "state"), "~/.local/state/caucionetta")
        shown = re.sub(r"bash=\S*/caucionetta ", "bash=caucionetta ", shown)  # machine-specific paths out of the docs
        (OUT / "swiftbar-output.txt").write_text(shown + "\n")
        with sync_playwright() as p:
            exe = os.environ.get("CHROMIUM_PATH")  # optional: an already-installed Chromium / headless shell
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            for dark in (False, True):
                pg = b.new_page(viewport={"width": 620, "height": 740}, device_scale_factor=2)
                pg.set_content(page(lines, dark))
                h = pg.evaluate("document.querySelector('.menu').getBoundingClientRect().bottom") + 24
                pg.set_viewport_size({"width": 620, "height": int(h)})
                pg.screenshot(path=str(OUT / f"swiftbar-{'dark' if dark else 'light'}.png"))
            b.close()
    print("\n".join(str(OUT / f"swiftbar-{m}.png") for m in ("light", "dark")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
