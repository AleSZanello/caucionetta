"""Safety rails: dry-run by default at every level, the double opt-in, the closed endpoint list, the limits, and
the demo mode staying away from real state."""
from __future__ import annotations

import datetime as dt
import os
import tomllib
from importlib import resources
from pathlib import Path

import pytest
from test_bot import TZ, FakeAPI

from caucionetta import api as iolapi
from caucionetta import bot, config, notify

T = dt.datetime(2030, 3, 12, 16, 51, tzinfo=TZ)  # fictional Tuesday, final placement window


@pytest.fixture
def notes(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: out.append(a))
    return out


def placed_after_run(cfg: dict, live: bool) -> list:
    api = FakeAPI({1: 36})
    bot.run(cfg, T, live=live, force=True, api=api)
    return api.placed


def test_the_default_config_is_dry_run():
    assert config.DEFAULTS["live"] is False
    assert config.load()["live"] is False


def test_the_example_config_matches_the_defaults():
    ex = tomllib.loads(resources.files("caucionetta").joinpath("config.example.toml").read_text())
    assert config.merge(config.DEFAULTS, ex) == config.DEFAULTS, "config.example.toml documents the real defaults"


@pytest.mark.parametrize("cfg_live,flag,expect", [(False, False, False), (True, False, False),
                                                   (False, True, False), (True, True, True)])
def test_real_orders_need_the_config_and_the_flag(cfg_live, flag, expect, notes):
    assert config.live_allowed({"live": cfg_live}, flag)[0] is expect
    assert bool(placed_after_run({"live": cfg_live}, flag)) is expect


def test_paused_wins_over_live(notes):
    bot.set_paused(True)
    assert placed_after_run({"live": True}, True) == []


def test_every_order_function_defaults_to_dry(notes):
    con = bot.db()
    api = FakeAPI({1: 36})
    c = bot.conf({})
    out = bot.cycle(api, con, c, T)  # no dry= argument
    assert out["dry"] is True and api.placed == []
    assert con.execute("select status from decisions").fetchone()[0] == "dry"
    o = {"order_id": "x1"}
    assert bot.cancel(api, con, o, T) is False and api.cancelled == []


def test_dry_run_logs_what_it_would_do(notes, caplog):
    caplog.set_level("INFO", logger="caucionetta")
    con = bot.db()
    bot.cycle(FakeAPI({1: 36}), con, bot.conf({}), T)
    assert any("DRY-RUN would place caución" in r.getMessage() for r in caplog.records)
    assert any("(simulada)" in n[0] for n in notes)


def test_forbidden_endpoints_never_reach_the_network():
    cl = iolapi.Client()
    for m, p in (("POST", "/broker/orders"), ("POST", "/broker/repos-borrowing"), ("GET", "/broker/withdrawals"),
                 ("POST", "/broker/transfers"), ("POST", "/broker/mutual-funds/subscriptions/../../orders"),
                 ("GET", "/broker/market-data/GGAL/bars"), ("GET", "/broker/accounts/US/balances")):
        with pytest.raises(iolapi.IOLError, match="no permitido"):
            cl._http1(m, p, auth=False)  # the conftest's urlopen would raise AssertionError if reached


def test_unconfigured_funds_are_refused():
    with pytest.raises(iolapi.IOLError):
        iolapi.Client(funds=[]).fund_subscribe("ANYFUND", 1000, "00000000-0000-4000-8000-000000000000")


def test_max_order_amount_and_keep_ars_cap_the_order(notes):
    con = bot.db()
    c = bot.conf({"bot": {"max_order_amount": 250_000, "keep_ars": 100_000}})
    api = FakeAPI({1: 36}, avail=1_000_000)
    bot.cycle(api, con, c, T, dry=False)
    assert [p["amount"] for p in api.placed] == [250_000]
    assert bot.available(api, c) == 650_000


def test_notifications_carry_no_amounts(notes):
    con = bot.db()
    bot.cycle(FakeAPI({1: 36}, avail=123_456), con, bot.conf({}), T, dry=False)
    assert notes and not any("123" in (n[0] + (n[1] if len(n) > 1 else "")) for n in notes)


def test_demo_mode_uses_its_own_state_and_no_config(monkeypatch, tmp_path):
    monkeypatch.delenv("CAUCIONETTA_STATE_DIR")
    monkeypatch.setenv("CAUCIONETTA_DEMO", "1")
    cfg_file = tmp_path / "real.toml"
    cfg_file.write_text("live = true\n")
    monkeypatch.setenv("CAUCIONETTA_CONFIG", str(cfg_file))
    assert config.load()["live"] is False, "the demo never reads your config"
    d = config.state_dir()
    assert d.name == "caucionetta-demo" and not str(d).startswith(str(Path.home() / ".local"))
    assert type(iolapi.make_client(config.load())).__name__ == "DemoAPI"
    from caucionetta import demo
    data = demo.seed()
    assert data["demo"] is True and data["money"]["total"] == 1_000_000
    os.environ.pop("CAUCIONETTA_DEMO", None)
