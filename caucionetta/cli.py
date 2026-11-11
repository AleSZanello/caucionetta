"""caucionetta — herramientas no oficiales para InvertirOnline (IOL): estado, tasas de caución, un bot de caución
colocadora (dry-run por defecto) y un servidor MCP. No es asesoramiento financiero."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from . import APP_NAME, __version__, actions, bot, config, credentials, notify, status
from . import api as iolapi

log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------- output helpers

def _setup_logging(verbose: bool, stderr_level=logging.INFO) -> None:
    root = logging.getLogger(APP_NAME)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    try:
        fh = logging.FileHandler(config.state_dir() / "bot.log")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.DEBUG if verbose else stderr_level)
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(sh)


def _emit(args, data, human) -> None:
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    else:
        human(data)


def money(v) -> str:
    if v is None:
        return "—"
    return "$ " + f"{float(v):,.0f}".replace(",", ".")


def pct(v) -> str:
    return "—" if v is None else f"{float(v):.1f}%".replace(".", ",")


def _api(cfg):
    return iolapi.make_client(cfg)


# ---------------------------------------------------------------- commands

def cmd_auth(args, cfg) -> int:
    service = cfg["auth"]["keychain_service"]
    if not credentials.keychain_available():
        print(f"No hay Keychain en este sistema: usá las variables {config.ENV_PREFIX}_USERNAME y "
              f"{config.ENV_PREFIX}_PASSWORD (por ejemplo desde tu gestor de contraseñas).")
        return 1
    if args.delete:
        n = credentials.keychain_delete(service)
        iolapi.Client(cfg).forget_tokens()
        print(f"Borré {n} ítem(s) del Keychain (servicio «{service}») y los tokens guardados.")
        return 0
    user = args.username or input("Usuario de IOL: ").strip()
    if not user:
        print("Sin usuario no guardo nada.")
        return 1
    credentials.keychain_set_username(service, user)
    print("Ahora `security` te pide la contraseña (dos veces). No pasa por caucionetta ni queda en el historial.")
    if not credentials.keychain_set_password_interactive(service):
        print("No se guardó la contraseña.")
        return 1
    iolapi.Client(cfg).forget_tokens()  # new credentials: log in again
    print(f"Guardado en el Keychain (servicio «{service}»). Probá con `caucionetta doctor --online`.")
    return 0


def _print_status(d: dict) -> None:
    m, y = d.get("money") or {}, d.get("yield") or {}
    tag = "  [DEMO: datos ficticios]" if d.get("demo") else ""
    print(f"caucionetta · {d.get('mode')} · mercado {d.get('market')} · {str(d.get('at'))[:16].replace('T', ' ')}{tag}")
    print(f"  Total        {money(m.get('total')):>14}")
    print(f"  En caución   {money(m.get('in_caucion')):>14}")
    print(f"  En el fondo  {money(m.get('in_fund')):>14}")
    print(f"  Libre        {money(m.get('free')):>14}")
    print(f"  Rinde        {pct(y.get('working_tna'))} TNA lo que trabaja · {money(y.get('per_day'))} por día")
    for c in d.get("cauciones") or []:
        print(f"  · caución {money(c['amount'])} al {pct(c['rate'])}, vence {c['due']} "
              f"({c['days']} día{'s' if c['days'] != 1 else ''}), +{money(c['interest'])}")
    if (d.get("fund") or {}).get("value"):
        f = d["fund"]
        print(f"  · fondo {f.get('symbol')} {money(f.get('value'))} al {pct(f.get('tna'))}")
    if d.get("today"):
        print("  Hoy:")
        for a in d["today"]:
            print(f"    {a['time']} {a['kind']:<8} {a['term']:<20} {pct(a['rate']):>7} {money(a['amount']):>12}  "
                  f"{a['status']} — {a['why']}")
    day = d.get("day") or {}
    print(f"  Día {day.get('kind')}{' (picos probables)' if day.get('high') else ''} · colocación final "
          f"{day.get('final_at') or '—'}")


def cmd_status(args, cfg) -> int:
    try:
        d = actions.read_status(cfg, _api(cfg) if args.refresh else None, args.refresh, args.live)
    except actions.ActionError as e:
        _emit(args, {"error": e.code, "message": str(e)}, lambda x: print(x["message"]))
        return 1
    except iolapi.IOLError as e:
        _emit(args, {"error": "iol", "message": str(e)}, lambda x: print(f"No: {x['message']}"))
        return 1
    _emit(args, d, _print_status)
    return 0


def cmd_cauciones(args, cfg) -> int:
    try:
        d = actions.cauciones(cfg, _api(cfg))
    except iolapi.IOLError as e:
        _emit(args, {"error": "iol", "message": str(e)}, lambda x: print(f"No: {x['message']}"))
        return 1

    def human(d):
        kinds = {"long_closure": "cierre largo", "month_end": "fin de mes", "season": "temporada", "friday": "viernes"}
        k = ", ".join(kinds[x] for x in d["day_kind"])
        print(f"Caución en {d['currency']} · {d['at'][:16].replace('T', ' ')} · día "
              f"{'de picos probables' if d['high_probability_day'] else 'normal'}{' (' + k + ')' if k else ''}"
              f" · colocación final {d['final_at']}")
        print(f"{'hábiles':>8} {'corridos':>9} {'vence':>11} {'tomador paga':>13} {'colocador pide':>15} {'referencia':>11}")
        for r in d["terms"]:
            print(f"{r['bdays']:>8} {r['cal_days']:>9} {r['due_date']:>11} {pct(r['taker_pays']):>13} "
                  f"{pct(r['lender_asks']):>15} {pct(r['baseline']):>11}")
    _emit(args, d, human)
    return 0


def _pass(args, cfg) -> dict:
    t = config.now_local(cfg)
    ok, why = config.live_allowed(cfg, args.live)
    con = bot.db()
    before = con.execute("select coalesce(max(rowid), 0) from decisions").fetchone()[0]
    seq = (bot._state().get("heartbeat") or {}).get("seq")
    con.close()
    code = bot.run(cfg, t=t, live=args.live, force=args.force, api=None)
    con = bot.db()
    try:
        decs = [dict(r) for r in con.execute("select ts, kind, bdays, cal_days, amount, rate, net_tna, status, reason,"
                                              " error, order_id from decisions where rowid>? order by rowid", (before,))]
    finally:
        con.close()
    hb = bot._state().get("heartbeat") or {}
    return {"exit": code, "at": t.isoformat(timespec="seconds"), "live": ok and not bot.paused(),
            "why": "pausado" if bot.paused() else why,
            "summary": hb.get("summary") if hb.get("seq") != seq else None, "decisions": decs}


def _print_pass(d: dict) -> None:
    print(f"{d['at'][:16].replace('T', ' ')} · {'EN VIVO' if d['live'] else 'SIMULADO (' + d['why'] + ')'}")
    print(f"  {d['summary'] or 'sin pasada (fuera de horario o todavía no toca: el bot se regula solo)'}")
    for x in d["decisions"]:
        verb = "colocaría" if x["status"] == "dry" else x["status"]
        plazo = bot.habiles(x["bdays"]) if x["bdays"] else "fondo"
        print(f"  → {verb}: {x['kind']} a {plazo}, {money(x['amount'])} con límite {pct(x['rate'])} "
              f"(neto {pct(x['net_tna'])}) — {x['reason']}" + (f" [{x['error']}]" if x["error"] else ""))


def cmd_bot(args, cfg) -> int:
    if args.what == "once":
        try:
            with config.lock("bot"):
                d = _pass(args, cfg)
        except config.Busy:
            print("Otra pasada del bot está corriendo.")
            return 0
        _emit(args, d, _print_pass)
        return d["exit"]
    if args.what == "run":
        import time
        print(f"Bot en primer plano cada {args.interval}s (Ctrl-C para salir). "
              f"{config.live_allowed(cfg, args.live)[1]}", flush=True)
        try:
            while True:
                try:
                    with config.lock("bot"):
                        d = _pass(args, cfg)
                    if d["decisions"] or not args.json:
                        _emit(args, d, _print_pass)
                    sys.stdout.flush()
                except config.Busy:
                    pass
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    if args.what in ("pause", "resume"):
        bot.set_paused(args.what == "pause")
        msg = "Pausado: mira y aprende, pero no coloca." if args.what == "pause" else \
            "Retomado (sigue haciendo falta live = true y --live para órdenes reales)."
        _emit(args, {"paused": bot.paused(), "message": msg}, lambda x: print(x["message"]))
        return 0
    if args.what == "stop":
        bot.set_paused(True)
        n = 0
        if cfg["live"]:  # orders can only exist if live was enabled; cancelling only reduces exposure
            api, con, t = _api(cfg), bot.db(), config.now_local(cfg)
            n = sum(1 for o in bot.open_orders(con, t) if bot.cancel(api, con, o, t, dry=False))
            n += bot.mm_unpark_today(api, con, t, dry=False)
            con.close()
        msg = f"Pausado. Cancelé {n} orden(es) pendiente(s) de hoy. `caucionetta bot resume` lo retoma."
        _emit(args, {"paused": True, "cancelled": n, "message": msg}, lambda x: print(x["message"]))
        return 0
    if args.what == "log":
        rows = actions.bot_log(args.days, cfg)

        def human(rows):
            if not rows:
                print("Sin decisiones en ese período.")
            for r in rows:
                print(f"{r['ts'][:16].replace('T', ' ')} {r['kind']:<6} {bot.habiles(r['bdays']) if r['bdays'] else 'fondo':<16} "
                      f"{money(r['amount']):>12} {pct(r['rate']):>7} (ref {pct(r['baseline'])}, neto {pct(r['net_tna'])}) "
                      f"{r['status']}{' [dry]' if r['dry'] else ''} — {r['reason']}" + (f" [{r['error']}]" if r["error"] else ""))
        _emit(args, rows, human)
        return 0
    if args.what == "learn":
        con = bot.db()
        lines = bot.learn(con, config.now_local(cfg))
        con.close()
        _emit(args, lines, lambda ls: [print("•", x) for x in ls or ["Todavía no hay datos suficientes."]])
        return 0
    return 2


def cmd_history(args, cfg) -> int:
    from . import a3
    if config.demo():
        print("En modo demo no se baja la historia de A3 (sin red).")
        return 0
    con = bot.db()
    since = dt.date.fromisoformat(args.since) if args.since else None
    n = a3.update(con, config.now_local(cfg).date(), since)
    print(f"A3: {n} filas de caución en pesos guardadas.")
    for bd in (1, 2, 3, 4, 5):
        h = bot.history(con, bd, config.now_local(cfg), cfg["bot"])
        if h["median"]:
            print(f"  {bot.habiles(bd)}: referencia {h['median']:.1f}% TNA ({h['source']}, {h['n']} datos)")
    return 0


def doctor_checks(cfg, online: bool) -> list[dict]:
    from . import launchd
    out = []

    def add(name, ok, detail):
        out.append({"check": name, "ok": ok, "detail": detail})
    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    add("config", True, cfg.get("_path") + ("" if config.demo() or os.path.exists(cfg["_path"]) else " (no existe: defaults)"))
    sd = config.state_dir()
    mode = oct(sd.stat().st_mode & 0o777)
    add("state dir", mode == "0o700", f"{sd} ({mode})")
    ok, why = config.live_allowed(cfg, True)
    add("modo", True, "live = true en el config (además hace falta --live)" if ok else "dry-run (live = false)")
    add("pausa", True, "pausado" if bot.paused() else "no")
    if config.demo():
        add("credenciales", True, "modo demo: no hacen falta")
    else:
        w = credentials.where(cfg)
        add("credenciales", all(w.values()), ", ".join(f"{k}: {v or 'falta'}" for k, v in w.items()))
    add("notificaciones", True, notify.backend())
    if sys.platform == "darwin":
        p = launchd.plist_path()
        add("launchd", True, f"instalado ({p})" if p.exists() else "no instalado (`caucionetta install-agent`)")
    hb = bot._state().get("heartbeat")
    add("última pasada", bool(hb) and bool(hb.get("ok")), f"{hb['at']}: {hb['summary']}" if hb else "nunca")
    st = status.read()
    add("status.json", st is not None, f"{status.path()} ({st['at']})" if st else "todavía no existe")
    add("log", True, str(sd / "bot.log"))
    if online:
        try:
            api = _api(cfg)
            api.token()
            n = len(api.terms(cfg["bot"]["currency"]))
            add("IOL", True, f"entra; {n} plazos de caución hoy")
        except iolapi.IOLError as e:
            add("IOL", False, str(e))
    return out


def cmd_doctor(args, cfg) -> int:
    checks = doctor_checks(cfg, args.online)

    def human(cs):
        for c in cs:
            print(f"  [{'ok' if c['ok'] else '!!'}] {c['check']:<15} {c['detail']}")
        if not args.online:
            print("  (sin --online no llamo a IOL)")
    _emit(args, checks, human)
    return 0 if all(c["ok"] for c in checks if c["check"] in ("python", "config", "IOL")) else 1


def cmd_install_agent(args, cfg) -> int:
    from . import launchd
    if sys.platform != "darwin":
        print("launchd es de macOS. En Linux usá cron o un timer de systemd (ver README, «Programarlo»).")
        return 1
    if args.uninstall:
        print("Desinstalado." if launchd.uninstall() else "No estaba instalado.")
        return 0
    if args.print:
        import plistlib
        sys.stdout.write(plistlib.dumps(launchd.build(args.live, args.interval)).decode())
        return 0
    p = launchd.install(args.live, args.interval, load=args.load)
    print(f"Escribí {p}.")
    if args.live and not cfg["live"]:
        print("Ojo: pusiste --live pero el config tiene live = false: va a correr en dry-run hasta que lo cambies.")
    if not args.load:
        print(f"Para activarlo: launchctl bootstrap gui/{os.getuid()} {p}")
    return 0


def cmd_mcp(args, cfg) -> int:
    from . import mcp
    _setup_logging(False, stderr_level=logging.WARNING)
    return mcp.Server(cfg, allow_live=args.allow_live, api_factory=lambda: _api(cfg)).serve()


def cmd_demo(args, cfg) -> int:
    os.environ[f"{config.ENV_PREFIX}_DEMO"] = "1"
    from . import demo
    d = demo.seed(reset=True)
    print(f"Demo lista en {config.state_dir()} (datos ficticios, sin red).")
    print(f"Probá: {config.ENV_PREFIX}_DEMO=1 caucionetta status   ·   {config.ENV_PREFIX}_DEMO=1 caucionetta bot once")
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    return 0


def cmd_config(args, cfg) -> int:
    if args.what == "path":
        print(config.config_path())
    elif args.what == "show":
        print(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}, ensure_ascii=False, indent=1))
    elif args.what == "init":
        from importlib import resources
        p = config.config_path()
        if p.exists():
            print(f"Ya existe: {p}")
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(resources.files(APP_NAME).joinpath("config.example.toml").read_text())
        os.chmod(p, 0o600)
        print(f"Escribí {p} (live = false). Editalo antes de usar el bot.")
    return 0


# ---------------------------------------------------------------- parser

def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="salida JSON para máquinas")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(prog=APP_NAME, description=__doc__, parents=[common])
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--config", help="ruta del config TOML (default: $CAUCIONETTA_CONFIG o ~/.config/caucionetta/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMANDO")

    p = sub.add_parser("auth", parents=[common], help="guardar usuario y contraseña de IOL en el Keychain")
    p.add_argument("--username")
    p.add_argument("--delete", action="store_true", help="borrar las credenciales y los tokens")
    p = sub.add_parser("status", parents=[common], help="el estado (del archivo local; --refresh llama a IOL)")
    p.add_argument("--refresh", action="store_true", help="rehacerlo ahora (2 llamadas a IOL)")
    p.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("cauciones", parents=[common], help="tasas de caución ahora, por plazo hábil")
    p = sub.add_parser("bot", parents=[common], help="el bot de caución (dry-run salvo live = true y --live)")
    p.add_argument("what", choices=["once", "run", "pause", "resume", "stop", "log", "learn"])
    p.add_argument("--live", action="store_true", help="permitir órdenes reales (además de live = true en el config)")
    p.add_argument("--force", action="store_true", help="pasada aunque el mercado esté cerrado o no toque todavía")
    p.add_argument("--interval", type=int, default=60, help="segundos entre pasadas para `bot run`")
    p.add_argument("--days", type=int, default=3, help="días para `bot log`")
    p = sub.add_parser("history", parents=[common], help="bajar la historia pública de A3 (referencias)")
    p.add_argument("--since")
    p = sub.add_parser("doctor", parents=[common], help="chequear instalación, config y credenciales")
    p.add_argument("--online", action="store_true", help="probar el login y leer los plazos (2 llamadas a IOL)")
    p = sub.add_parser("install-agent", parents=[common], help="programar el bot con launchd (macOS)")
    p.add_argument("--live", action="store_true")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--load", action="store_true", help="activarlo ya (launchctl bootstrap)")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--print", action="store_true", help="mostrar el plist sin escribirlo")
    p = sub.add_parser("mcp", parents=[common], help="servidor MCP por stdio")
    p.add_argument("--allow-live", action="store_true", help="permitir apply_proposal (además de live = true)")
    sub.add_parser("demo", parents=[common], help="armar la demo con datos ficticios")
    p = sub.add_parser("config", parents=[common], help="config: path | show | init")
    p.add_argument("what", choices=["path", "show", "init"])
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.json = getattr(args, "json", False)
    args.verbose = getattr(args, "verbose", False)
    if args.config:
        os.environ[f"{config.ENV_PREFIX}_CONFIG"] = args.config
    if args.cmd == "demo":
        os.environ[f"{config.ENV_PREFIX}_DEMO"] = "1"
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"Config inválido: {e}", file=sys.stderr)
        return 2
    notify.setup(cfg)
    if args.cmd != "mcp":
        _setup_logging(args.verbose, stderr_level=logging.INFO if args.cmd == "bot" and args.verbose else logging.WARNING)
    if config.demo() and args.cmd != "demo":
        from . import demo
        demo.ensure_seeded()
    handlers = {"auth": cmd_auth, "status": cmd_status, "cauciones": cmd_cauciones, "bot": cmd_bot,
                "history": cmd_history, "doctor": cmd_doctor, "install-agent": cmd_install_agent, "mcp": cmd_mcp,
                "demo": cmd_demo, "config": cmd_config}
    return handlers[args.cmd](args, cfg)
