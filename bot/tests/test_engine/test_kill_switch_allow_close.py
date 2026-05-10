"""Tests for kill-switch allowing CLOSE signals (audit H3).

Pre-fix: `check_risk_limits()` returned False uniformly for all signals
when `KILL_SWITCH=true`, freezing position-already-open bots — strategy
SL/TP exits couldn't fire.

Post-fix: `check_risk_limits(is_open=False)` skips the kill-switch and
daily-loss gates because closing a position can only REDUCE risk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.portfolio import PortfolioManager


@pytest.mark.asyncio
async def test_kill_switch_blocks_open(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=None)
    assert await pm.check_risk_limits(is_open=True) is False


@pytest.mark.asyncio
async def test_kill_switch_allows_close(monkeypatch):
    """Even with kill-switch ON, a CLOSE signal must proceed so SL/TP
    exits keep firing. Closing reduces exposure; that's never the bug."""
    monkeypatch.setattr(settings, "kill_switch", True)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=None)
    assert await pm.check_risk_limits(is_open=False) is True


@pytest.mark.asyncio
async def test_daily_loss_blocks_open_only(monkeypatch):
    """A CLOSE signal must proceed even when the daily-loss cap is hit
    — otherwise a sustained loss would freeze positions instead of
    realizing them and capping further damage."""
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=None)
    pm._daily_pnl = -150.0
    pm._loaded = True
    assert await pm.check_risk_limits(is_open=True) is False
    assert await pm.check_risk_limits(is_open=False) is True


@pytest.mark.asyncio
async def test_default_is_open_true_for_back_compat(monkeypatch):
    """The default `is_open=True` preserves the pre-fix behavior for
    callers that haven't been updated yet (defensive default)."""
    monkeypatch.setattr(settings, "kill_switch", True)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)
    pm = PortfolioManager(exchange=MagicMock(), control=None)
    assert await pm.check_risk_limits() is False
