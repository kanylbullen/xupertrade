"""Tests for /link command + chat-routing exception (PR 3b).

The /link handler is the only command allowed from chats that
aren't the operator's pre-configured one — proves chat-ownership
during initial tenant onboarding. Tests verify:

- Bad code formats are rejected before Redis lookup
- Missing/expired codes return a clear error
- Valid codes trigger repo.upsert_telegram_link + key cleanup
- Malformed tenant_id in Redis is surfaced gracefully
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.notify.telegram import TelegramNotifier


def _notifier(repo=None, redis_=None) -> TelegramNotifier:
    """Build a bare TelegramNotifier without wiring start()."""
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "fake"
    n._chat_id = "1"  # operator's chat
    n._control = MagicMock()
    n._mainnet_control = None
    n._exchange = None
    n._strategies = []
    n._strategy_by_name = {}
    n._repo = repo
    n._redis = redis_
    return n


@pytest.mark.asyncio
async def test_link_with_invalid_format_returns_usage():
    notif = _notifier(repo=MagicMock(), redis_=MagicMock())
    # Too few digits
    msg = await notif._cmd_link(["12345"], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()
    # Non-numeric
    msg = await notif._cmd_link(["abcdef"], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()
    # No args at all
    msg = await notif._cmd_link([], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_expired_code_returns_error():
    redis_ = MagicMock()
    redis_.get = AsyncMock(return_value=None)  # key gone (expired or never existed)
    notif = _notifier(repo=MagicMock(), redis_=redis_)
    msg = await notif._cmd_link(["123456"], chat_id="9999", username="alice")
    assert "invalid" in msg.lower() or "expired" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_valid_code_upserts_and_cleans_up():
    tenant_id = uuid.uuid4()
    redis_ = MagicMock()
    redis_.get = AsyncMock(return_value=str(tenant_id))
    redis_.delete = AsyncMock()
    repo = MagicMock()
    repo.upsert_telegram_link = AsyncMock()
    notif = _notifier(repo=repo, redis_=redis_)

    msg = await notif._cmd_link(["654321"], chat_id="9999", username="alice")
    assert "Linked" in msg or "✅" in msg

    repo.upsert_telegram_link.assert_awaited_once_with(
        tenant_id=tenant_id,
        telegram_chat_id=9999,
        telegram_username="alice",
    )
    # Both forward + reverse keys deleted on successful link
    redis_.delete.assert_awaited_once()
    args = redis_.delete.await_args[0]
    assert "tg-link:654321" in args
    assert f"tg-link:tenant:{tenant_id}" in args


@pytest.mark.asyncio
async def test_link_with_malformed_tenant_id_in_redis_returns_error():
    redis_ = MagicMock()
    redis_.get = AsyncMock(return_value="not-a-uuid")
    notif = _notifier(repo=MagicMock(), redis_=redis_)
    msg = await notif._cmd_link(["111222"], chat_id="9999", username="alice")
    assert "Internal error" in msg or "corrupted" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_no_repo_returns_unavailable():
    notif = _notifier(repo=None, redis_=MagicMock())
    msg = await notif._cmd_link(["123456"], chat_id="9999", username="alice")
    assert "unavailable" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_no_chat_id_returns_internal_error():
    notif = _notifier(repo=MagicMock(), redis_=MagicMock())
    msg = await notif._cmd_link(["123456"], chat_id=None, username="alice")
    assert "Internal error" in msg or "chat" in msg.lower()


@pytest.mark.asyncio
async def test_link_continues_when_redis_cleanup_fails():
    """Redis cleanup error must NOT make the user think linking failed —
    the DB upsert already succeeded so we just log and return success."""
    tenant_id = uuid.uuid4()
    redis_ = MagicMock()
    redis_.get = AsyncMock(return_value=str(tenant_id))
    redis_.delete = AsyncMock(side_effect=RuntimeError("redis down"))
    repo = MagicMock()
    repo.upsert_telegram_link = AsyncMock()
    notif = _notifier(repo=repo, redis_=redis_)

    msg = await notif._cmd_link(["654321"], chat_id="9999", username="alice")
    # User-visible message reports success even though cleanup failed
    assert "Linked" in msg or "✅" in msg
    repo.upsert_telegram_link.assert_awaited_once()
