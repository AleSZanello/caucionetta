"""Every test runs isolated: its own state dir, no config file, no Keychain, no network, no notifications."""
from __future__ import annotations

import urllib.request

import pytest

from caucionetta import credentials, notify


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k in (
        "CAUCIONETTA_DEMO",
        "CAUCIONETTA_USERNAME",
        "CAUCIONETTA_PASSWORD",
        "CAUCIONETTA_TELEGRAM_BOT_TOKEN",
        "CAUCIONETTA_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CAUCIONETTA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CAUCIONETTA_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(credentials, "keychain_get", lambda *a, **k: None)

    def no_network(*a, **k):
        raise AssertionError("tests never touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(notify, "_macos", lambda *a, **k: None)
    yield
