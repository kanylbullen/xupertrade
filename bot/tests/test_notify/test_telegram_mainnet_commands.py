"""Tests for Telegram /-mainnet commands (audit C4).

Pre-fix: TelegramNotifier was constructed with the LOCAL bot's
BotControl (testnet's, since Telegram lives on the testnet bot). The
operator's `/pause` and `/flat confirm` wrote to testnet-namespaced
Redis keys. The mainnet bot polls `hypertrade:mainnet:control:*` and
never saw them.

Post-fix: TelegramNotifier accepts a `mainnet_control` handle pointing at
mainnet's namespace, and exposes `/pause-mainnet`, `/resume-mainnet`,
`/flat-mainnet`, `/status-mainnet` that route through it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.notify.telegram import TelegramNotifier


def _notifier(mainnet_control=None) -> TelegramNotifier:
    """Build a notifier with the network session pre-stubbed."""
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "fake"
    n._chat_id = "1"
    n._control = MagicMock()
    n._mainnet_control = mainnet_control
    n._exchange = None
    n._strategies = []
    n._strategy_by_name = {}
    n._repo = None
    return n


@pytest.mark.asyncio
async def test_pause_mainnet_writes_to_mainnet_control():
    """/pause-mainnet must call set_paused(True) on the MAINNET BotControl,
    not on the local one."""
    mainnet = MagicMock()
    mainnet.set_paused = AsyncMock()
    notif = _notifier(mainnet_control=mainnet)
    msg = await notif._cmd_pause_mainnet([])
    mainnet.set_paused.assert_awaited_once_with(True)
    assert "MAINNET" in msg
    # Local control must NOT be touched
    notif._control.set_paused.assert_not_called()


@pytest.mark.asyncio
async def test_resume_mainnet_writes_to_mainnet_control():
    mainnet = MagicMock()
    mainnet.set_paused = AsyncMock()
    notif = _notifier(mainnet_control=mainnet)
    msg = await notif._cmd_resume_mainnet([])
    mainnet.set_paused.assert_awaited_once_with(False)
    assert "MAINNET" in msg
    notif._control.set_paused.assert_not_called()


@pytest.mark.asyncio
async def test_flat_mainnet_requires_confirm():
    """A bare /flat-mainnet must NOT trigger flat-all — operator must
    repeat with `confirm` to avoid fat-fingering an emergency-close."""
    mainnet = MagicMock()
    mainnet.request_flat_all = AsyncMock()
    notif = _notifier(mainnet_control=mainnet)
    msg = await notif._cmd_flat_mainnet([])
    mainnet.request_flat_all.assert_not_called()
    assert "confirm" in msg


@pytest.mark.asyncio
async def test_flat_mainnet_with_confirm_writes_to_mainnet_control():
    """`/flat-mainnet confirm` writes a token to mainnet's
    flat_request_id — the mainnet bot's runner picks it up and unwinds."""
    mainnet = MagicMock()
    mainnet.request_flat_all = AsyncMock()
    notif = _notifier(mainnet_control=mainnet)
    msg = await notif._cmd_flat_mainnet(["confirm"])
    mainnet.request_flat_all.assert_awaited_once()
    # The token should be in the response (first 8 chars truncated)
    assert "MAINNET" in msg
    # Local control must not be touched
    notif._control.request_flat_all.assert_not_called()


@pytest.mark.asyncio
async def test_status_mainnet_reads_from_mainnet_control():
    """Status pulls paused, disabled, heartbeat from MAINNET keys only."""
    import time as _time
    mainnet = MagicMock()
    mainnet.is_paused = AsyncMock(return_value=True)
    mainnet.get_disabled_strategies = AsyncMock(return_value={"penguin_volatility"})
    mainnet.get_heartbeat = AsyncMock(return_value=int(_time.time()) - 30)
    notif = _notifier(mainnet_control=mainnet)
    msg = await notif._cmd_status_mainnet([])
    assert "PAUSED" in msg
    assert "penguin_volatility" in msg
    assert "30s ago" in msg
    notif._control.is_paused.assert_not_called()


@pytest.mark.asyncio
async def test_mainnet_commands_degrade_when_not_wired():
    """A testnet-only deployment (no mainnet bot) leaves
    `mainnet_control=None`. Commands must report cleanly, not crash."""
    notif = _notifier(mainnet_control=None)
    for fn, args in [
        (notif._cmd_pause_mainnet, []),
        (notif._cmd_resume_mainnet, []),
        (notif._cmd_flat_mainnet, ["confirm"]),
        (notif._cmd_status_mainnet, []),
    ]:
        msg = await fn(args)
        assert "not wired" in msg, f"{fn.__name__} did not degrade gracefully"


@pytest.mark.asyncio
async def test_status_mainnet_stale_heartbeat():
    """Heartbeat older than 180s should render with the warning marker."""
    import time as _time
    mainnet = MagicMock()
    mainnet.is_paused = AsyncMock(return_value=False)
    mainnet.get_disabled_strategies = AsyncMock(return_value=set())
    mainnet.get_heartbeat = AsyncMock(return_value=int(_time.time()) - 600)
    notif = _notifier(mainnet_control=mainnet)
    msg = await notif._cmd_status_mainnet([])
    assert "stale" in msg
