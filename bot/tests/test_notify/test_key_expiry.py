"""Tests for the HL private-key expiry reminder loop."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.notify.telegram import TelegramNotifier


TENANT_ID = "11111111-2222-3333-4444-555555555555"


def _notifier(rows: list[tuple[str, datetime]]):
    """Build a notifier with `rows` staged as the injected expiry env.

    Expiries now arrive via the TENANT_KEY_EXPIRIES env var (a JSON
    object) instead of a `tenant_secrets` query — the bot's PG role has
    no grant on that table, so the old DB read failed on every run.
    Assigning `settings.tenant_key_expiries` here is safe because the
    autouse fixture monkeypatches it first, so pytest restores the
    original value at teardown.
    """
    settings.tenant_key_expiries = json.dumps(
        {key: expires.isoformat() for key, expires in rows}
    )
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "fake"
    n._chat_id = "12345"
    n._session = MagicMock()
    n._enabled_types = set()
    n._repo = MagicMock()
    n._redis = MagicMock()
    n._redis.set = AsyncMock(return_value=True)
    n.send = AsyncMock(return_value=True)
    return n


@pytest.fixture(autouse=True)
def _set_tenant(monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", TENANT_ID)
    # Registers the pre-test value so _notifier's direct assignment is
    # rolled back after each test.
    monkeypatch.setattr(settings, "tenant_key_expiries", "")


@pytest.mark.asyncio
async def test_in_window_sends_warning():
    expires = datetime.now(timezone.utc) + timedelta(days=10)
    n = _notifier([("HYPERLIQUID_PRIVATE_KEY", expires)])
    await n._check_key_expiries()
    n.send.assert_awaited_once()
    msg = n.send.await_args.args[0]
    assert "expires in" in msg
    assert "HYPERLIQUID_PRIVATE_KEY" in msg
    n._redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_not_yet_in_window_is_silent():
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    n = _notifier([("HYPERLIQUID_PRIVATE_KEY", expires)])
    await n._check_key_expiries()
    n.send.assert_not_called()


@pytest.mark.asyncio
async def test_expired_first_time_sends_with_dedup_set():
    expires = datetime.now(timezone.utc) - timedelta(days=1)
    n = _notifier([("HYPERLIQUID_MAINNET_PRIVATE_KEY", expires)])
    await n._check_key_expiries()
    n.send.assert_awaited_once()
    assert "EXPIRED" in n.send.await_args.args[0]
    # Dedup key was claimed
    n._redis.set.assert_awaited_once()
    kwargs = n._redis.set.await_args.kwargs
    assert kwargs.get("nx") is True
    assert kwargs.get("ex") == TelegramNotifier.EXPIRED_NOTIFIED_TTL_SECONDS


@pytest.mark.asyncio
async def test_expired_already_notified_does_not_resend():
    expires = datetime.now(timezone.utc) - timedelta(days=5)
    n = _notifier([("HYPERLIQUID_PRIVATE_KEY", expires)])
    # SET NX returns None/False when the key already exists
    n._redis.set = AsyncMock(return_value=None)
    await n._check_key_expiries()
    n.send.assert_not_called()


@pytest.mark.asyncio
async def test_no_tenant_skips(monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", None)
    n = _notifier([("HYPERLIQUID_PRIVATE_KEY",
                   datetime.now(timezone.utc) + timedelta(days=5))])
    await n._check_key_expiries()
    n.send.assert_not_called()


@pytest.mark.asyncio
async def test_no_redis_skips():
    n = _notifier([("HYPERLIQUID_PRIVATE_KEY",
                   datetime.now(timezone.utc) + timedelta(days=5))])
    n._redis = None
    await n._check_key_expiries()
    n.send.assert_not_called()
