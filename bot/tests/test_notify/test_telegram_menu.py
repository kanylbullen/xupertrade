"""Tests for the Telegram bot-command menu publish (setMyCommands)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

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
async def test_publish_menu_skips_invalid_command_names(caplog):
    """Telegram's BotCommand.command spec is `[a-z0-9_]{1,32}`. A single
    invalid name (e.g. a hyphen) makes the WHOLE setMyCommands call fail
    with HTTP 400. The publish path filters client-side so a stray
    invalid name only loses that one command, not the entire menu.
    Regression test from PR #29 deploy: `/status-mainnet` (hyphen) broke
    the menu publish until renamed to `/status_mainnet`."""
    captured: dict = {}
    class _FakeResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def json(self): return {"ok": True}

    def _fake_post(url, **kwargs):
        captured["json"] = kwargs.get("json")
        return _FakeResp()

    session = MagicMock()
    session.post = _fake_post

    notif = _notifier_with_session(session)
    notif._commands = {
        "/ok_one": (MagicMock(), "Valid one"),
        "/bad-hyphen": (MagicMock(), "Invalid: contains hyphen"),
        "/UPPER": (MagicMock(), "Invalid: uppercase"),
        "/ok_two": (MagicMock(), "Valid two"),
    }
    with caplog.at_level(logging.WARNING, logger="hypertrade.notify.telegram"):
        await notif._publish_command_menu()

    cmd_names = {c["command"] for c in captured["json"]["commands"]}
    assert cmd_names == {"ok_one", "ok_two"}, (
        f"only valid commands should be published; got: {cmd_names}"
    )
    skipped = [r.message for r in caplog.records if "Skipping invalid" in r.message]
    assert len(skipped) == 2, f"expected 2 skip warnings; got: {skipped}"


@pytest.mark.asyncio
async def test_publish_menu_skips_call_when_all_commands_invalid(caplog):
    """Telegram treats setMyCommands with an empty array as 'delete all
    commands'. If a bug filters every command out, we must NOT make the
    call — otherwise a transient typo would silently wipe the working
    menu. Short-circuit + warning instead."""
    posted = {"called": False}

    def _fake_post(url, **kwargs):
        posted["called"] = True
        raise AssertionError("setMyCommands must not be called when commands is empty")

    session = MagicMock()
    session.post = _fake_post

    notif = _notifier_with_session(session)
    notif._commands = {
        "/UPPER": (MagicMock(), "Invalid: uppercase"),
        "/has-hyphen": (MagicMock(), "Invalid: hyphen"),
    }
    with caplog.at_level(logging.WARNING, logger="hypertrade.notify.telegram"):
        await notif._publish_command_menu()
    assert posted["called"] is False
    assert any("0 valid commands" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_publish_menu_logs_warning_on_non_200(caplog):
    """Non-200 responses log a warning AND don't raise. Verifies both
    via caplog assertion (PR #25 review fix — was only checking
    no-raise, name implied warning verification too)."""
    class _FakeResp:
        status = 401
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def text(self): return "Unauthorized"

    session = MagicMock()
    session.post = lambda url, **kw: _FakeResp()
    notif = _notifier_with_session(session)
    with caplog.at_level(logging.WARNING, logger="hypertrade.notify.telegram"):
        await notif._publish_command_menu()  # must not raise
    assert any(
        "setMyCommands HTTP 401" in r.message and r.levelname == "WARNING"
        for r in caplog.records
    ), f"expected warning log on 401; got: {[r.message for r in caplog.records]}"
