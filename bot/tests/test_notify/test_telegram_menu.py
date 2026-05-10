"""Tests for the Telegram bot-command menu publish (setMyCommands)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hypertrade.notify.telegram import TelegramNotifier


def _notifier_with_session(session_mock):
    """Build a TelegramNotifier with the network session pre-stubbed.

    Skips __init__ since real init reads settings + redis URL; tests
    only need the in-memory `_commands` dict and `_session` mock.
    """
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "fake-token"
    n._chat_id = "12345"
    n._session = session_mock
    n._commands = {
        "/start": (MagicMock(), "Show this help message"),
        "/help": (MagicMock(), "Show this help message"),  # dedupe target
        "/status": (MagicMock(), "Bot status"),
        "/positions": (MagicMock(), "Open positions"),
    }
    return n


@pytest.mark.asyncio
async def test_publish_menu_calls_setMyCommands_with_deduped_list():
    """Verify the API call goes to setMyCommands with the right URL and
    that /start + /help (same description) appear only once."""
    captured: dict = {}

    class _FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def json(self): return {"ok": True}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _FakeResp()

    session = MagicMock()
    session.post = _fake_post

    notif = _notifier_with_session(session)
    await notif._publish_command_menu()

    assert "setMyCommands" in captured["url"]
    assert "fake-token" in captured["url"]
    cmds = captured["json"]["commands"]
    # Dedupe: /start and /help share description → only one survives
    assert len(cmds) == 3
    names = {c["command"] for c in cmds}
    # No leading slash in the API payload
    assert all(not c["command"].startswith("/") for c in cmds)
    assert "status" in names and "positions" in names


@pytest.mark.asyncio
async def test_publish_menu_truncates_long_descriptions():
    """Telegram's setMyCommands hard-limits description to 256 chars."""
    notif = _notifier_with_session(MagicMock())
    long_desc = "x" * 500
    notif._commands = {"/foo": (MagicMock(), long_desc)}

    captured = {}
    class _FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def json(self): return {"ok": True}
    notif._session.post = lambda url, **kw: (captured.update(kw), _FakeResp())[1]

    await notif._publish_command_menu()
    assert len(captured["json"]["commands"][0]["description"]) == 256


@pytest.mark.asyncio
async def test_publish_menu_swallows_network_error():
    """API failures are non-fatal — startup must continue."""
    session = MagicMock()
    session.post = MagicMock(side_effect=RuntimeError("network down"))
    notif = _notifier_with_session(session)
    # Must not raise
    await notif._publish_command_menu()


@pytest.mark.asyncio
async def test_publish_menu_logs_warning_on_non_200():
    """Non-200 responses log a warning but don't raise."""
    class _FakeResp:
        status = 401
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def text(self): return "Unauthorized"

    session = MagicMock()
    session.post = lambda url, **kw: _FakeResp()
    notif = _notifier_with_session(session)
    # Must not raise
    await notif._publish_command_menu()
