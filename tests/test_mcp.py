"""The MCP server: protocol over real stdio, and the propose → apply flow."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from test_bot import FakeAPI

from caucionetta import actions, bot, config, mcp, notify


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)


def rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def call(server, name, args=None):
    r = rpc(server, "tools/call", {"name": name, "arguments": args or {}})["result"]
    return r["isError"], json.loads(r["content"][0]["text"])


def make(live_cfg=True, allow_live=True, api=None):
    cfg = config.merge(config.DEFAULTS, {"live": live_cfg})
    api = api or FakeAPI({1: 36, 2: 37})
    return mcp.Server(cfg, allow_live=allow_live, api_factory=lambda: api), api


def test_stdio_initialize_and_tools_list(tmp_path):
    env = dict(os.environ, CAUCIONETTA_DEMO="1", CAUCIONETTA_STATE_DIR=str(tmp_path / "s"))
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}}]
    p = subprocess.run([sys.executable, "-m", "caucionetta", "mcp"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    lines = [json.loads(x) for x in p.stdout.splitlines()]
    assert [x["id"] for x in lines] == [1, 2, 3], "the notification gets no answer"
    assert lines[0]["result"]["protocolVersion"] == "2025-06-18"
    assert lines[0]["result"]["serverInfo"]["name"] == "caucionetta"
    names = {t["name"] for t in lines[1]["result"]["tools"]}
    assert {"get_status", "get_cauciones", "get_positions_summary", "propose_caucion", "apply_proposal"} <= names
    assert lines[2]["result"]["structuredContent"]["demo"] is True


def test_unknown_method_and_tool():
    s, _ = make()
    assert rpc(s, "nope")["error"]["code"] == -32601
    assert rpc(s, "tools/call", {"name": "buy_everything"})["error"]["code"] == -32602


def test_propose_previews_and_sends_nothing():
    s, api = make()
    err, r = call(s, "propose_caucion", {"bdays": 1, "amount": 100_000})
    assert not err and r["confirmation_id"] and r["expires_in_s"] == 120
    p = r["preview"]
    assert (p["bdays"], p["amount"], p["limit_rate_pct"]) == (1, 100_000, 36)
    assert p["fees_estimate"] > 0 and p["live_enabled"] is True
    assert api.placed == []


def test_apply_executes_once_with_the_id():
    s, api = make()
    _, r = call(s, "propose_caucion", {"bdays": 2, "amount": 50_000, "rate_pct": 37})
    err, out = call(s, "apply_proposal", {"confirmation_id": r["confirmation_id"]})
    assert not err and out["executed"] and out["status"] == "sent"
    assert [(p["term_id"], p["amount"], p["rate"]) for p in api.placed] == [(102, 50_000, 37)]
    err, out = call(s, "apply_proposal", {"confirmation_id": r["confirmation_id"]})
    assert err and out["error"] == "unknown_proposal", "single use"
    assert len(api.placed) == 1


@pytest.mark.parametrize("live_cfg,allow_live", [(False, True), (True, False), (False, False)])
def test_apply_refuses_without_live_mode(live_cfg, allow_live):
    s, api = make(live_cfg, allow_live)
    _, r = call(s, "propose_caucion", {"bdays": 1, "amount": 100_000})
    assert r["preview"]["live_enabled"] is False and r["preview"]["warnings"]
    err, out = call(s, "apply_proposal", {"confirmation_id": r["confirmation_id"]})
    assert err and out["error"] == "live_disabled"
    assert api.placed == []


def test_apply_refuses_when_paused_and_pause_is_a_tool():
    s, api = make()
    _, r = call(s, "propose_caucion", {"bdays": 1, "amount": 100_000})
    err, out = call(s, "pause_bot")
    assert not err and bot.paused()
    err, out = call(s, "apply_proposal", {"confirmation_id": r["confirmation_id"]})
    assert err and out["error"] == "paused" and api.placed == []


def test_proposals_expire():
    clock = [1000.0]
    store = actions.Proposals(ttl_s=60, clock=lambda: clock[0])
    cid = store.add("caucion", {}, {})["confirmation_id"]
    clock[0] += 61
    with pytest.raises(actions.ActionError) as e:
        store.take(cid)
    assert e.value.code == "proposal_expired"


def test_propose_validates_amounts():
    s, _ = make(api=FakeAPI({1: 36}, avail=10_000))
    err, out = call(s, "propose_caucion", {"bdays": 1, "amount": 50_000})
    assert err and "pesos libres" in out["message"]
    err, out = call(s, "propose_caucion", {"bdays": 9, "amount": 5_000})
    assert err and out["error"] == "invalid"


def test_cancel_goes_through_propose_and_apply():
    s, api = make()
    _, r = call(s, "propose_caucion", {"bdays": 1, "amount": 100_000})
    _, placed = call(s, "apply_proposal", {"confirmation_id": r["confirmation_id"]})
    _, c = call(s, "propose_cancel", {"order_id": placed["order_id"]})
    assert api.cancelled == []
    err, out = call(s, "apply_proposal", {"confirmation_id": c["confirmation_id"]})
    assert not err and out["executed"] and api.cancelled == [placed["order_id"]]


def test_breaker_blocks_manual_orders_too():
    s, api = make()
    con = bot.db()
    for i in range(3):
        con.execute("insert into decisions (id, day, status) values (?,?, 'error')",
                    (f"e{i}", config.now_local().date().isoformat()))
    con.commit()
    _, r = call(s, "propose_caucion", {"bdays": 1, "amount": 100_000})
    err, out = call(s, "apply_proposal", {"confirmation_id": r["confirmation_id"]})
    assert err and out["error"] == "breaker" and api.placed == []
