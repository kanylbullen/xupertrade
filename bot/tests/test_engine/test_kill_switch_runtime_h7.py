"""Tests for runtime kill-switch via Redis (audit H7).

Pre-fix: `settings.kill_switch` was env-only — flipping it required
`docker compose restart`, during which the running tick could still
place orders.

Post-fix: `BotControl.{is,set}_kill_switch_active` reads/writes the
override in Redis, and `PortfolioManager.check_risk_limits` consults
the override before falling back to the env default. New API endpoint
`/api/control/kill-switch` lets the operator flip it without restarting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.portfolio import PortfolioManager


def _make_control(kill_switch_state, kill_switch_raises=False):
    """BotControl mock with the daily_pnl stubs PortfolioManager
    requires (PR #32 review fix — without these the first await on
    control.get_daily_pnl raised TypeError, which the production code
    swallowed but obscured what the test was actually exercising)."""
    control = MagicMock()
    control.get_daily_pnl = AsyncMock(return_value=0.0)
    control.set_daily_pnl = AsyncMock()
    if kill_switch_raises:
        control.is_kill_switch_active = AsyncMock(
            side_effect=RuntimeError("redis down"),
        )
    else:
        control.is_kill_switch_active = AsyncMock(return_value=kill_switch_state)
    return control


@pytest.mark.asyncio
async def test_redis_unset_falls_back_to_env(monkeypatch):
    """Override unset → env wins. env=true → no opens."""
    monkeypatch.setattr(settings, "kill_switch", True)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=_make_control(None))
    assert await pm.check_risk_limits(is_open=True) is False


@pytest.mark.asyncio
async def test_redis_override_true_blocks_when_env_false(monkeypatch):
    """env=false but Redis=true (operator activated at runtime) → blocks."""
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=_make_control(True))
    assert await pm.check_risk_limits(is_open=True) is False


@pytest.mark.asyncio
async def test_redis_override_false_allows_when_env_true(monkeypatch):
    """env=true but Redis=false (operator deactivated at runtime) → allows.
    This is the headline use case — flip to false without restart."""
    monkeypatch.setattr(settings, "kill_switch", True)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=_make_control(False))
    assert await pm.check_risk_limits(is_open=True) is True


@pytest.mark.asyncio
async def test_redis_read_failure_falls_back_to_env(monkeypatch):
    """Redis blip during the kill-switch read must NOT crash the engine
    tick. Fall back to the env default and log."""
    monkeypatch.setattr(settings, "kill_switch", True)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(
        exchange=MagicMock(),
        control=_make_control(None, kill_switch_raises=True),
    )
    # env=true → blocks
    assert await pm.check_risk_limits(is_open=True) is False


@pytest.mark.asyncio
async def test_close_signals_unaffected_by_runtime_kill_switch(monkeypatch):
    """Even with Redis kill-switch=true, CLOSE signals proceed (audit H3
    interaction). Closing reduces risk; that's never the bug."""
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=_make_control(True))
    assert await pm.check_risk_limits(is_open=False) is True
