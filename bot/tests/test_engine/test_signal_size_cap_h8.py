"""Tests for signal.size notional ceiling (audit H8).

Pre-fix: vvv_hedge emitted `Signal(size=400)` and the engine used 400
verbatim — no clamp. An accidental param bump (`holding_vvv` 400 → 4000)
silently produced a 10× position with the same hard SL.

Post-fix: a hard ceiling rejects opens whose `size × current_price`
exceeds `signal_size_max_multiplier × MAX_POSITION_SIZE_USD`. CLOSE
signals and signals without size override are unaffected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction


def _runner():
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=[])
    repo.get_open_position = AsyncMock(return_value=None)
    repo.get_open_position_any = AsyncMock(return_value=None)

    portfolio = MagicMock()
    portfolio.check_risk_limits = AsyncMock(return_value=True)

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=True)

    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)
    exchange.place_order = AsyncMock()  # tracker for asserting NOT called

    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=None, control=control,
    )
    runner.portfolio = portfolio
    return runner


@pytest.mark.asyncio
async def test_size_within_cap_passes(monkeypatch):
    """vvv_hedge's design: 400 VVV × $5 = $2000 notional. With default
    cap (10× × $200 = $2000), this is at the boundary and must pass."""
    monkeypatch.setattr(settings, "max_position_size_usd", 200)
    monkeypatch.setattr(settings, "signal_size_max_multiplier", 10.0)
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)  # disable that gate
    runner = _runner()
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="VVV",
        strategy_name="vvv_hedge", size=400.0,
    )
    # Use a MagicMock returning Order() to keep the path going past the gate
    from hypertrade.exchange.base import Order, OrderStatus, OrderType
    runner.exchange.place_order = AsyncMock(return_value=Order(
        id="x", symbol="VVV", side="buy", size=400.0,
        order_type=OrderType.MARKET, filled_price=5.0,
        status=OrderStatus.FILLED,
    ))
    runner.repo.record_trade_and_open_position = AsyncMock()
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    await runner._execute_signal(sig, current_price=5.0, leverage=1)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_size_above_cap_rejected(monkeypatch):
    """The accidental-bump scenario: 4000 VVV × $5 = $20k. With cap
    $2k, must reject without placing the order."""
    monkeypatch.setattr(settings, "max_position_size_usd", 200)
    monkeypatch.setattr(settings, "signal_size_max_multiplier", 10.0)
    runner = _runner()
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="VVV",
        strategy_name="vvv_hedge", size=4000.0,
    )
    ok = await runner._execute_signal(sig, current_price=5.0, leverage=1)
    assert ok is False
    runner.exchange.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_close_signals_unaffected_by_cap(monkeypatch):
    """CLOSE_LONG with a giant signal.size must NOT be cap-blocked —
    closing reduces exposure; the size goes through resolve-close-size
    anyway. We just need to confirm the H8 gate isn't in the CLOSE path."""
    monkeypatch.setattr(settings, "max_position_size_usd", 200)
    monkeypatch.setattr(settings, "signal_size_max_multiplier", 10.0)
    runner = _runner()
    # _resolve_close_size would normally be called; stub it.
    runner._resolve_close_size = AsyncMock(return_value=None)  # short-circuit AFTER cap
    sig = Signal(
        action=SignalAction.CLOSE_LONG, symbol="VVV",
        strategy_name="vvv_hedge", size=99999.0,
    )
    # Should NOT be blocked by H8 gate. _resolve_close_size returning None
    # makes _execute_signal return False — but the cap log line MUST NOT
    # have fired. We verify by checking place_order wasn't called and the
    # control flow reached _resolve_close_size.
    await runner._execute_signal(sig, current_price=5.0, leverage=1)
    runner._resolve_close_size.assert_awaited_once()


@pytest.mark.asyncio
async def test_calculated_size_unaffected_by_cap(monkeypatch):
    """Signals without size override (most strategies) go through
    _calculate_size which is already capped by MAX_POSITION_SIZE_USD.
    The H8 check must not fire when `signal.size` is None."""
    monkeypatch.setattr(settings, "max_position_size_usd", 200)
    monkeypatch.setattr(settings, "signal_size_max_multiplier", 10.0)
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    runner = _runner()
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="BTC",
        strategy_name="bb_short", size=None,
    )
    from hypertrade.exchange.base import Order, OrderStatus, OrderType
    runner.exchange.place_order = AsyncMock(return_value=Order(
        id="x", symbol="BTC", side="buy", size=0.004,
        order_type=OrderType.MARKET, filled_price=50_000.0,
        status=OrderStatus.FILLED,
    ))
    runner.repo.record_trade_and_open_position = AsyncMock()
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    await runner._execute_signal(sig, current_price=50_000.0, leverage=1)
    runner.exchange.place_order.assert_awaited()
