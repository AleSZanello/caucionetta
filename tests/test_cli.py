"""The CLI in demo mode (fictional data, no network)."""
from __future__ import annotations

import json
import plistlib

from caucionetta import cli, launchd


def run(capsys, monkeypatch, tmp_path, *argv):
    monkeypatch.setenv("CAUCIONETTA_DEMO", "1")
    monkeypatch.setenv("CAUCIONETTA_STATE_DIR", str(tmp_path / "demo"))
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_help_and_version(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert "caucionetta" in capsys.readouterr().out


def test_demo_status_json(capsys, monkeypatch, tmp_path):
    code, out = run(capsys, monkeypatch, tmp_path, "status", "--json")
    d = json.loads(out)
    assert code == 0 and d["demo"] and d["mode"] == "simulado"


def test_demo_bot_once_is_a_dry_run(capsys, monkeypatch, tmp_path):
    code, out = run(capsys, monkeypatch, tmp_path, "--json", "bot", "once", "--live")
    d = json.loads(out)
    assert code == 0 and d["live"] is False and "live = true" in d["why"]
    assert [x["status"] for x in d["decisions"]] == ["dry"]


def test_doctor_and_cauciones(capsys, monkeypatch, tmp_path):
    assert run(capsys, monkeypatch, tmp_path, "doctor")[0] == 0
    code, out = run(capsys, monkeypatch, tmp_path, "cauciones", "--json")
    assert code == 0 and len(json.loads(out)["terms"]) == 5


def test_launchd_plist_needs_explicit_live(monkeypatch):
    p = launchd.build(live=False)
    assert p["ProgramArguments"][-2:] == ["bot", "once"]
    assert launchd.build(live=True)["ProgramArguments"][-1] == "--live"
    plistlib.dumps(p)
