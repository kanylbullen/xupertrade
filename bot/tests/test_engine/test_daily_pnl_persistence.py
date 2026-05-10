"""Tests for daily-loss kill-switch surviving container restart (audit C2).

Pre-fix: `PortfolioManager._daily_pnl` was an in-memory float that reset
to 0.0 on every restart. After a $400 loss a `docker compose restart`
zeroed the counter and trading resumed despite blowing through
`MAX_DAILY_LOSS_USD=100`.

Post-fix: PnL is mirrored to Redis via BotControl with a mode-namespaced
key + 48h TTL, loaded on first risk check, written on every record_pnl.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.portfolio import PortfolioManager


@pytest.mark.asyncio
async def test_record_pnl_persists_to_control():
    """Each record_pnl call must mirror the running total to BotControl
    so a restart can restore it. Without this, the daily-loss cap is a
    no-op."""
    control = MagicMock()
    control.get_daily_pnl = AsyncMock(return_value=0.0)
    control.set_daily_pnl = AsyncMock()
    pm = PortfolioManager(exchange=MagicMock(), control=control)

    await pm.record_pnl(-50.0)
    await pm.record_pnl(-30.0)

    # Both writes happened with the running cumulative total
    assert control.set_daily_pnl.await_count == 2
    last_call = control.set_daily_pnl.await_args_list[-1]
    # second call: date_str + cumulative pnl (-80.0)
    assert last_call.args[1] == pytest.approx(-80.0)


@pytest.mark.asyncio
async def test_check_risk_limits_loads_persisted_pnl_on_first_call():
    """A restart begins with `_loaded=False`. The first call to
    `check_risk_limits` must pull today's PnL from Redis so a lossy day
    isn't silently forgiven."""
    control = MagicMock()
    control.get_daily_pnl = AsyncMock(return_value=-150.0)
    control.set_daily_pnl = AsyncMock()
    pm = PortfolioManager(exchange=MagicMock(), control=control)

    # Default cap is $100 in test settings — but the important thing is
    # that the loaded value is reflected, not zero.
    from hypertrade.config import settings

    settings.max_daily_loss_usd = 100  # ensure the cap is hit
    settings.kill_switch = False

    allowed = await pm.check_risk_limits()
    assert allowed is False
    assert pm._daily_pnl == -150.0
    control.get_daily_pnl.assert_awaited_once()


@pytest.mark.asyncio
async def test_simulated_restart_resumes_with_persisted_loss():
    """End-to-end: PortfolioManager A records a loss → reads back what A
    persisted from a freshly-constructed PortfolioManager B with the
    same control. B must see the loss."""
    # Shared store mocking Redis behavior
    store: dict[str, float] = {}

    async def _get(date_str: str) -> float:
        return store.get(date_str, 0.0)

    async def _set(date_str: str, pnl: float) -> None:
        store[date_str] = pnl

    control = MagicMock()
    control.get_daily_pnl = AsyncMock(side_effect=_get)
    control.set_daily_pnl = AsyncMock(side_effect=_set)

    from hypertrade.config import settings
    settings.max_daily_loss_usd = 100
    settings.kill_switch = False

    pm_before = PortfolioManager(exchange=MagicMock(), control=control)
    await pm_before.record_pnl(-120.0)

    # Simulate restart — fresh PortfolioManager, same Redis (control)
    pm_after = PortfolioManager(exchange=MagicMock(), control=control)
    allowed = await pm_after.check_risk_limits()
    assert allowed is False, "post-restart must still see yesterday's loss"
    assert pm_after._daily_pnl == pytest.approx(-120.0)


@pytest.mark.asyncio
async def test_no_control_falls_back_to_in_memory():
    """Without BotControl (paper-without-redis, tests) the manager
    silently tracks PnL in-memory. Cap still works within one process."""
    pm = PortfolioManager(exchange=MagicMock(), control=None)

    from hypertrade.config import settings
    settings.max_daily_loss_usd = 100
    settings.kill_switch = False

    await pm.record_pnl(-150.0)
    allowed = await pm.check_risk_limits()
    assert allowed is False


@pytest.mark.asyncio
async def test_set_daily_pnl_failure_does_not_raise():
    """If Redis write fails (network blip), the in-memory counter must
    still update and the call must not propagate the error — otherwise
    the trade-record path crashes after a successful order."""
    control = MagicMock()
    control.get_daily_pnl = AsyncMock(return_value=0.0)
    control.set_daily_pnl = AsyncMock(side_effect=RuntimeError("redis down"))
    pm = PortfolioManager(exchange=MagicMock(), control=control)

    # Must not raise
    await pm.record_pnl(-25.0)
    assert pm._daily_pnl == pytest.approx(-25.0)
