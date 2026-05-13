"""Tests for /link command + chat-routing exception (PR 3b).

The /link handler is the only command allowed from chats that
aren't the operator's pre-configured one — proves chat-ownership
during initial tenant onboarding. Tests verify:

- Bad code formats are rejected
- Missing/expired codes return a clear error
- Valid codes trigger repo.upsert_telegram_link + key cleanup
- Malformed tenant_id in Redis is surfaced gracefully
- Per-chat sliding-window rate-limit blocks 6th attempt (M-1)
- Old 6-digit codes are rejected with an upgrade-prompt message
- Lowercase + whitespace in the code is normalised
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.notify.telegram import TelegramNotifier


# Valid 10-char codes in the Crockford alphabet [A-HJ-NP-Z2-9].
VALID_CODE_1 = "ABCDE23456"
VALID_CODE_2 = "XYZJK98765"
VALID_CODE_3 = "PQRST34567"
VALID_CODE_4 = "FGHJM45678"


def _rl_pipeline(count: int = 1, ttl: int = 1800):
    """Mock async pipeline returning rate-limit results matching
    the (incr, expire, ttl) shape check_rate_limit expects."""
    pipe = MagicMock()
    pipe.incr = MagicMock()
    pipe.expire = MagicMock()
    pipe.ttl = MagicMock()
    pipe.execute = AsyncMock(return_value=[count, 1, ttl])
    return pipe


def _notifier(repo=None, redis_=None) -> TelegramNotifier:
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "fake"
    n._chat_id = "1"
    n._control = MagicMock()
    n._mainnet_control = None
    n._exchange = None
    n._strategies = []
    n._strategy_by_name = {}
    n._repo = repo
    n._redis = redis_
    return n


def _redis_with_rl(*, getdel_return=None, delete_side_effect=None,
                   pipeline_count: int = 1, pipeline_ttl: int = 1800):
    r = MagicMock()
    r.pipeline = MagicMock(return_value=_rl_pipeline(pipeline_count, pipeline_ttl))
    r.getdel = AsyncMock(return_value=getdel_return)
    if delete_side_effect is not None:
        r.delete = AsyncMock(side_effect=delete_side_effect)
    else:
        r.delete = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_link_with_invalid_format_returns_usage():
    notif = _notifier(repo=MagicMock(), redis_=_redis_with_rl())
    msg = await notif._cmd_link(["12345"], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()
    msg = await notif._cmd_link(["ABCDEFGHJKLM"], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()
    # I is forbidden
    msg = await notif._cmd_link(["ABCDEIJKLM"], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()
    msg = await notif._cmd_link([], chat_id="9999", username="alice")
    assert "Usage" in msg or "code" in msg.lower()


@pytest.mark.asyncio
async def test_link_old_6digit_code_returns_upgrade_message():
    """M-1: 6-digit codes are no longer minted; surface an explicit
    'mint a fresh code' message rather than the generic usage hint."""
    notif = _notifier(repo=MagicMock(), redis_=_redis_with_rl())
    msg = await notif._cmd_link(["123456"], chat_id="9999", username="alice")
    assert "no longer supported" in msg.lower() or "10 characters" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_expired_code_returns_error():
    redis_ = _redis_with_rl(getdel_return=None)
    notif = _notifier(repo=MagicMock(), redis_=redis_)
    msg = await notif._cmd_link([VALID_CODE_1], chat_id="9999", username="alice")
    assert "invalid" in msg.lower() or "expired" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_valid_code_upserts_and_cleans_up():
    tenant_id = uuid.uuid4()
    redis_ = _redis_with_rl(getdel_return=str(tenant_id))
    repo = MagicMock()
    repo.upsert_telegram_link = AsyncMock()
    notif = _notifier(repo=repo, redis_=redis_)

    msg = await notif._cmd_link([VALID_CODE_2], chat_id="9999", username="alice")
    assert "Linked" in msg or "✅" in msg

    repo.upsert_telegram_link.assert_awaited_once_with(
        tenant_id=tenant_id,
        telegram_chat_id=9999,
        telegram_username="alice",
    )
    redis_.getdel.assert_awaited_once_with(f"tg-link:{VALID_CODE_2}")
    redis_.delete.assert_awaited_once_with(f"tg-link:tenant:{tenant_id}")


@pytest.mark.asyncio
async def test_link_accepts_lowercase_and_whitespace():
    """M-1: case-insensitive + strip whitespace so users copying from
    the dashboard don't trip on stray spaces."""
    tenant_id = uuid.uuid4()
    redis_ = _redis_with_rl(getdel_return=str(tenant_id))
    repo = MagicMock()
    repo.upsert_telegram_link = AsyncMock()
    notif = _notifier(repo=repo, redis_=redis_)

    msg = await notif._cmd_link(
        [f"  {VALID_CODE_3.lower()}  "], chat_id="9999", username="alice"
    )
    assert "Linked" in msg or "✅" in msg
    redis_.getdel.assert_awaited_once_with(f"tg-link:{VALID_CODE_3}")


@pytest.mark.asyncio
async def test_link_with_malformed_tenant_id_in_redis_returns_error():
    redis_ = _redis_with_rl(getdel_return="not-a-uuid")
    notif = _notifier(repo=MagicMock(), redis_=redis_)
    msg = await notif._cmd_link([VALID_CODE_1], chat_id="9999", username="alice")
    assert "Internal error" in msg or "corrupted" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_no_repo_returns_unavailable():
    notif = _notifier(repo=None, redis_=_redis_with_rl())
    msg = await notif._cmd_link([VALID_CODE_1], chat_id="9999", username="alice")
    assert "unavailable" in msg.lower()


@pytest.mark.asyncio
async def test_link_with_no_chat_id_returns_internal_error():
    notif = _notifier(repo=MagicMock(), redis_=_redis_with_rl())
    msg = await notif._cmd_link([VALID_CODE_1], chat_id=None, username="alice")
    assert "Internal error" in msg or "chat" in msg.lower()


@pytest.mark.asyncio
async def test_link_continues_when_redis_cleanup_fails():
    tenant_id = uuid.uuid4()
    redis_ = _redis_with_rl(
        getdel_return=str(tenant_id),
        delete_side_effect=RuntimeError("redis down"),
    )
    repo = MagicMock()
    repo.upsert_telegram_link = AsyncMock()
    notif = _notifier(repo=repo, redis_=redis_)

    msg = await notif._cmd_link([VALID_CODE_4], chat_id="9999", username="alice")
    assert "Linked" in msg or "✅" in msg
    repo.upsert_telegram_link.assert_awaited_once()


# --- M-1 per-chat rate-limit -----------------------------------------------


@pytest.mark.asyncio
async def test_link_rate_limit_blocks_sixth_attempt_in_window():
    """5 attempts allowed per 30 min per chat; the 6th hard-fails
    with a 'try again in N min' message instead of being silently
    dropped (which the old global-cooldown bucket did)."""
    redis_ = MagicMock()
    repo = MagicMock()
    repo.upsert_telegram_link = AsyncMock()

    counts = iter([1, 2, 3, 4, 5, 6])

    def make_pipeline():
        return _rl_pipeline(count=next(counts), ttl=1800)

    redis_.pipeline = MagicMock(side_effect=make_pipeline)
    redis_.getdel = AsyncMock(return_value=None)

    notif = _notifier(repo=repo, redis_=redis_)

    for _ in range(5):
        msg = await notif._cmd_link([VALID_CODE_1], chat_id="42", username="a")
        assert "invalid" in msg.lower() or "expired" in msg.lower()

    msg = await notif._cmd_link([VALID_CODE_1], chat_id="42", username="a")
    assert "too many" in msg.lower() or "try again" in msg.lower()
    assert redis_.getdel.await_count == 5


@pytest.mark.asyncio
async def test_link_rate_limit_is_per_chat_not_global():
    """One chat exhausting its quota does NOT block other chats —
    the bug in the old global-cooldown bucket."""
    redis_ = MagicMock()
    repo = MagicMock()

    per_key_count: dict[str, int] = {}
    seen_keys: list[str] = []

    def make_pipeline():
        pipe = MagicMock()

        def _incr(key):
            seen_keys.append(key)
            per_key_count[key] = per_key_count.get(key, 0) + 1

        pipe.incr = MagicMock(side_effect=_incr)
        pipe.expire = MagicMock()
        pipe.ttl = MagicMock()

        async def _execute():
            current_key = seen_keys[-1]
            return [per_key_count[current_key], 1, 1800]

        pipe.execute = _execute
        return pipe

    redis_.pipeline = MagicMock(side_effect=make_pipeline)
    redis_.getdel = AsyncMock(return_value=None)

    notif = _notifier(repo=repo, redis_=redis_)

    for _ in range(5):
        await notif._cmd_link([VALID_CODE_1], chat_id="attacker", username=None)
    blocked = await notif._cmd_link([VALID_CODE_1], chat_id="attacker", username=None)
    assert "too many" in blocked.lower() or "try again" in blocked.lower()

    legit = await notif._cmd_link([VALID_CODE_2], chat_id="legit", username=None)
    assert "expired" in legit.lower() or "invalid" in legit.lower()
    assert per_key_count["ratelimit:tg-link-attempt:legit"] == 1
    assert per_key_count["ratelimit:tg-link-attempt:attacker"] == 6


@pytest.mark.asyncio
async def test_link_rate_limit_resets_after_window():
    """When the Redis key expires, the count goes back to 1 and
    attempts succeed again."""
    redis_ = MagicMock()
    repo = MagicMock()

    counts = iter([6, 1])

    def make_pipeline():
        return _rl_pipeline(count=next(counts), ttl=1800)

    redis_.pipeline = MagicMock(side_effect=make_pipeline)
    redis_.getdel = AsyncMock(return_value=None)

    notif = _notifier(repo=repo, redis_=redis_)

    blocked = await notif._cmd_link([VALID_CODE_1], chat_id="42", username=None)
    assert "too many" in blocked.lower() or "try again" in blocked.lower()

    after = await notif._cmd_link([VALID_CODE_1], chat_id="42", username=None)
    assert "too many" not in after.lower()
