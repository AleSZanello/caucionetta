#!/usr/bin/env python3
# <xbar.title>caucionetta</xbar.title>
# <xbar.version>v0.1.0</xbar.version>
# <xbar.author>caucionetta contributors</xbar.author>
# <xbar.desc>Caución bot status from caucionetta's local status file. Never calls IOL. Unofficial.</xbar.desc>
# <xbar.dependencies>python3,caucionetta</xbar.dependencies>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""SwiftBar / xbar plugin for caucionetta.

It ONLY reads <state>/status.json (written by the bot), so opening the menu never calls IOL. "Actualizar" runs
`caucionetta status --refresh` (2 calls). Copy or symlink this file into your SwiftBar plugin folder; the `1m` in
the name is how often SwiftBar re-reads the file.

  CAUCIONETTA_STATE_DIR   where the status lives (default ~/.local/state/caucionetta, or the demo dir with CAUCIONETTA_DEMO=1)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP = "caucionetta"
GREEN, ORANGE, GRAY, RED = "#2e9d5b,#5fd38d", "#c9731c,#f0a35c", "#8a8a8a,#9a9a9a", "#c0392b,#ff6b5e"


def state_dir() -> Path:
    if os.environ.get("CAUCIONETTA_STATE_DIR"):
        return Path(os.environ["CAUCIONETTA_STATE_DIR"]).expanduser()
    if os.environ.get("CAUCIONETTA_DEMO"):
        return Path(tempfile.gettempdir()) / f"{APP}-demo"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / APP


def caucionetta_bin() -> str:
    path = os.pathsep.join([os.environ.get("PATH", ""), str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                            "/usr/local/bin"])
    return shutil.which(APP, path=path) or APP


def money(v, hide: bool) -> str:
    if v is None:
        return "—"
    if hide:
        return "$ •••"
    return "$ " + f"{float(v):,.0f}".replace(",", ".")


def pct(v) -> str:
    return "—" if v is None else f"{float(v):.1f}%".replace(".", ",")


def hhmm(iso) -> str:
    return str(iso or "")[11:16] or "—"


def esc(s) -> str:
    return str(s).replace("|", "¦")  # a pipe would start SwiftBar's parameters


def render(d: dict | None, hide: bool, sd: Path, exe: str) -> list[str]:
    out = []
    if not d:
        out += [f"{APP} | sfimage=chart.line.uptrend.xyaxis", "---",
                "Todavía no hay datos | color=" + GRAY,
                "El bot escribe el estado en su próxima pasada. | size=11 color=" + GRAY]
    else:
        y, m = d.get("yield") or {}, d.get("money") or {}
        mode = d.get("mode") or "—"
        title = pct(y.get("working_tna")) if y.get("working_tna") else APP
        out.append(f"{title} | sfimage=chart.line.uptrend.xyaxis")
        out.append("---")
        badge = " · DEMO" if d.get("demo") else ""
        color = GREEN if mode == "en vivo" else ORANGE
        out.append(f"Cauciones · {esc(mode)}{badge} | color={color} size=13")
        out.append(f"Total  {money(m.get('total'), hide)} | font=Menlo size=13")
        out.append(f"En caución  {money(m.get('in_caucion'), hide)} | font=Menlo size=12 color={GRAY}")
        out.append(f"En el fondo  {money(m.get('in_fund'), hide)} | font=Menlo size=12 color={GRAY}")
        out.append(f"Libre  {money(m.get('free'), hide)} | font=Menlo size=12 color={GRAY}")
        out.append(f"Rinde {pct(y.get('working_tna'))} TNA · {money(y.get('per_day'), hide)} por día | size=12")
        live = d.get("cauciones") or []
        if live:
            out.append("---")
            out.append("Trabajando ahora | size=11 color=" + GRAY)
            for c in live:
                out.append(f"{money(c['amount'], hide)} al {pct(c['rate'])} · vence {c['due'][8:10]}/{c['due'][5:7]} "
                           f"· +{money(c['interest'], hide)} | font=Menlo size=12 sfimage=lock.fill")
        f = d.get("fund") or {}
        if f.get("value"):
            out.append(f"{money(f['value'], hide)} en {esc(f.get('symbol'))} al {pct(f.get('tna'))} | font=Menlo size=12 "
                       "sfimage=parkingsign.circle")
        today = d.get("today") or []
        out.append("---")
        out.append(f"Hoy ({len(today)}) | size=11 color={GRAY}")
        for a in today[-6:]:
            col = GREEN if a.get("ok") else ORANGE
            out.append(f"{a['time']} {esc(a['kind'])} {esc(a['term'])} {pct(a.get('rate'))} · {esc(a['status'])} "
                       f"| size=12 color={col}")
            out.append(f"--{esc(a.get('error') or a.get('why'))} | size=11")
        if not today:
            out.append(f"Sin movimientos todavía | size=12 color={GRAY}")
        rates = d.get("rates") or []
        if rates:
            day = d.get("day") or {}
            out.append("---")
            out.append(f"Tasas · día {esc(day.get('kind'))} · coloca {day.get('final_at') or '—'} | size=11 color={GRAY}")
            for r in rates:
                hot = r.get("take") and r.get("ref") and r["take"] >= r["ref"] * 1.5
                label = "1 día hábil" if r["bdays"] == 1 else f"{r['bdays']} días hábiles"
                out.append(f"{label:<16} {pct(r.get('take')):>6}  ref {pct(r.get('ref'))} | font=Menlo size=12"
                           + (f" color={ORANGE}" if hot else ""))
        out.append("---")
        if d.get("stale"):
            out.append(f"Ojo: datos de hace más de una hora (¿corre el bot?) | size=11 color={ORANGE}")
        out.append(f"Actualizado {hhmm(d.get('at'))} · mercado {esc(d.get('market'))} | size=11 color={GRAY}")
    out.append(f"Actualizar | bash={exe} param1=status param2=--refresh terminal=false refresh=true sfimage=arrow.clockwise")
    if d and d.get("mode") == "pausado":
        out.append(f"Reanudar | bash={exe} param1=bot param2=resume terminal=false refresh=true sfimage=play.fill")
    else:
        out.append(f"Pausar | bash={exe} param1=bot param2=pause terminal=false refresh=true sfimage=pause.fill")
    flag = sd / "bar-hide-amounts"
    if hide:
        out.append(f"Mostrar montos | bash=/bin/rm param1=-f param2={flag} terminal=false refresh=true sfimage=eye")
    else:
        out.append(f"Ocultar montos | bash=/usr/bin/touch param1={flag} terminal=false refresh=true sfimage=eye.slash")
    out.append(f"Abrir el log | bash=/usr/bin/open param1=-t param2={sd / 'bot.log'} terminal=false sfimage=doc.text")
    out.append("No oficial · no es asesoramiento financiero | size=10 color=" + GRAY)
    return out


def main() -> int:
    sd = state_dir()
    try:
        d = json.loads((sd / "status.json").read_text())
    except (OSError, ValueError):
        d = None
    if d and d.get("at"):
        try:
            age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(d["at"])
            d["stale"] = age.total_seconds() > 3600 and not d.get("demo")
        except ValueError:
            pass
    hide = (sd / "bar-hide-amounts").exists() or os.environ.get("CAUCIONETTA_HIDE_AMOUNTS") == "1"
    print("\n".join(render(d, hide, sd, caucionetta_bin())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
