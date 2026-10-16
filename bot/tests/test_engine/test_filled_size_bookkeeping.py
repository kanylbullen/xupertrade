"""Bookkeeping uses the FILLED size, not the requested one.

HyperLiquid rounds every order to the coin's szDecimals, and `Order.size`
carries the filled amount (PR #170). The runner used its own local
`size` — the unrounded request — for the fee, the realised PnL, the
trade and position rows and the TradeExecuted event, so the DB drifted
from the exchange by the rounding on every trade: the live 247×
"size mismatch" reconcile warning (`analysis-2026-09-15.md` § 4).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Order, OrderStatus, OrderType, Position

REQUESTED = 0.002523
FILLED = 0.00252
PRICE = 100_000.0
FEE_RATE = 0.00045


def _runner(filled_size, *, open_row=None):
    repo = MagicMock()
    repo.get_open_position = AsyncMock(return_value=open_row)
    repo.get_open_positions = AsyncMock(return_value=[open_row] if open_row else [])
    repo.record_trade_and_open_position = AsyncMock()
    repo.record_trade_and_close_position = AsyncMock()

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=False)
    control.save_strategy_state = AsyncMock()

    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)
    # The exchange holds the open row, if any: a close against a flat
    # exchange is booked as an external close instead (NU-5a).
    exchange.get_position = AsyncMock(return_value=open_row and Position(
        symbol=open_row.symbol, side=open_row.side, size=open_row.size,
        entry_price=open_row.entry_price,
    ))
    # BTC: the request and the fill differ by rounding, not a short fill.
    exchange.get_size_precision = MagicMock(return_value=5)

    async def _place(symbol, side, size, order_type=OrderType.MARKET, **kw):
        return Order(
            id="oid-1", symbol=symbol, side=side, size=filled_size,
            order_type=order_type, filled_price=PRICE,
            status=OrderStatus.FILLED,
        )

    exchange.place_order = AsyncMock(side_effect=_place)
    bus = MagicMock()
    bus.publish = AsyncMock()

    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=bus, control=control,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.check_risk_limits = AsyncMock(return_value=True)
    runner.portfolio.record_pnl = AsyncMock()
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    return runner, repo, exchange, bus


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    monkeypatch.setattr(settings, "taker_fee_rate", FEE_RATE)


def _trade_executed(bus):
    return next(
        c.args[0] for c in bus.publish.await_args_list
        if c.args[0].type == "trade.executed"
    )


@pytest.mark.asyncio
async def test_open_books_the_filled_size():
    runner, repo, exchange, bus = _runner(FILLED)
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="BTC",
        strategy_name="kalman_breakout", size=REQUESTED,
    )

    ok = await runner._execute_signal(sig, current_price=PRICE, leverage=1)

    assert ok is True
    assert exchange.place_order.await_args.args[2] == REQUESTED, "we ask for the request"
    kw = repo.record_trade_and_open_position.await_args.kwargs
    assert kw["size"] == FILLED
    assert kw["fee"] == pytest.approx(PRICE * FILLED * FEE_RATE)
    assert _trade_executed(bus).size == FILLED


@pytest.mark.asyncio
async def test_close_books_the_filled_size_and_pnl():
    row = MagicMock()
    row.strategy_name = "kalman_breakout"
    row.symbol = "BTC"
    row.side = "long"
    row.size = REQUESTED
    row.entry_price = 90_000.0
    runner, repo, exchange, bus = _runner(FILLED, open_row=row)
    sig = Signal(
        action=SignalAction.CLOSE_LONG, symbol="BTC",
        strategy_name="kalman_breakout",
    )

    ok = await runner._execute_signal(sig, current_price=PRICE, leverage=1)

    assert ok is True
    assert exchange.place_order.await_args.args[2] == REQUESTED
    kw = repo.record_trade_and_close_position.await_args.kwargs
    fee = PRICE * FILLED * FEE_RATE
    assert kw["size"] == FILLED
    assert kw["fee"] == pytest.approx(fee)
    assert kw["pnl"] == pytest.approx((PRICE - 90_000.0) * FILLED - fee)
    runner.portfolio.record_pnl.assert_awaited_once_with(kw["pnl"])
    assert _trade_executed(bus).size == FILLED
