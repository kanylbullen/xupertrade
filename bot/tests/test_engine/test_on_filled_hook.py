"""Engine wiring for Strategy.on_filled (oleg paper-loop "Fix C").

After a successful OPEN fill, _execute_signal calls strategy.on_filled with
the ACTUAL exchange fill price (not the bar close / current_price), BEFORE
it persists the strategy's state — so state_json captures corrected
brackets. A strategy whose on_filled throws must NOT break the open.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Order, OrderStatus, OrderType
from hypertrade.strategies.base import Strategy


class _RecordingStrategy(Strategy):
    name = "recording"
    symbol = "ETH"

    def __init__(self, **kw):
        super().__init__(**kw)
        self._entry_price = None
        self.on_filled_calls = []

    async def on_candle(self, candles):  # type: ignore[override]
        return None

    def on_filled(self, side, fill_price):
        self.on_filled_calls.append((side, fill_price))
        self._entry_price = fill_price

    def export_state(self):
        if self._entry_price is None:
            return None
        return {"entry_price": self._entry_price}


class _BrokenStrategy(_RecordingStrategy):
    name = "broken"

    def on_filled(self, side, fill_price):
        raise RuntimeError("bracket re-derivation blew up")


def _runner(strategy):
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=[])
    repo.get_open_position = AsyncMock(return_value=None)
    repo.get_open_position_any = AsyncMock(return_value=None)
    repo.record_trade_and_open_position = AsyncMock()

    portfolio = MagicMock()
    portfolio.check_risk_limits = AsyncMock(return_value=True)

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=True)
    control.save_strategy_state = AsyncMock()

    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)

    runner = EngineRunner(
        exchange=exchange, strategies=[strategy], repo=repo,
        event_bus=None, control=control,
    )
    runner.portfolio = portfolio
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    return runner


def _fill_order(price):
    return Order(
        id="o1", symbol="ETH", side="buy", size=1.0,
        order_type=OrderType.MARKET, filled_price=price,
        status=OrderStatus.FILLED,
    )


@pytest.mark.asyncio
async def test_on_filled_called_with_exchange_fill_not_bar_close(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    strat = _RecordingStrategy()
    runner = _runner(strat)
    # Bar close / current_price = 2000, but the exchange fills at 2010.
    runner.exchange.place_order = AsyncMock(return_value=_fill_order(2010.0))
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH",
        strategy_name="recording", size=1.0,
    )
    ok = await runner._execute_signal(sig, current_price=2000.0, leverage=1)
    assert ok is True
    assert strat.on_filled_calls == [("long", 2010.0)]


@pytest.mark.asyncio
async def test_broken_on_filled_does_not_break_open(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    strat = _BrokenStrategy()
    runner = _runner(strat)
    runner.exchange.place_order = AsyncMock(return_value=_fill_order(2010.0))
    sig = Signal(
        action=SignalAction.OPEN_SHORT, symbol="ETH",
        strategy_name="broken", size=1.0,
    )
    # Must NOT raise — the position is already open.
    ok = await runner._execute_signal(sig, current_price=2000.0, leverage=1)
    assert ok is True
    runner.repo.record_trade_and_open_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_persisted_state_reflects_post_fill_brackets(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    strat = _RecordingStrategy()
    runner = _runner(strat)
    runner.exchange.place_order = AsyncMock(return_value=_fill_order(2010.0))
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH",
        strategy_name="recording", size=1.0,
    )
    await runner._execute_signal(sig, current_price=2000.0, leverage=1)

    call = runner.repo.record_trade_and_open_position.await_args
    state_json = call.kwargs["state_json"]
    state = json.loads(state_json)
    # The persisted entry must be the FILL price (2010), not the bar close.
    assert state["entry_price"] == 2010.0
