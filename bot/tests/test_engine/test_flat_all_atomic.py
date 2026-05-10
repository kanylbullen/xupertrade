"""Tests for atomic flat-all + DB entry-price PnL (audit H5+H6).

Pre-fix:
- H5 — `_flat_all_positions` called `repo.record_trade` and
  `repo.close_position` in TWO separate sessions. SIGTERM/crash
  between them left the Trade row recorded with no `is_open=false`
  update; reconcile then orphan-closed with PnL=0, double-counting.
- H6 — Realized PnL was computed against the EXCHANGE entry_price,
  which is a volume-weighted average across all add-to-position legs.
  If two strategies opened on the same coin, the recorded PnL was
  wrong by the leg-difference.

Post-fix:
- Lookup the open DB position FIRST and use its entry_price for PnL.
- Use `repo.record_trade_and_close_position` (single transaction).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.runner import EngineRunner
from hypertrade.exchange.base import Order, OrderStatus, OrderType, Position


def _runner_with(exchange_pos, db_pos):
    repo = MagicMock()
    repo.get_open_position_any = AsyncMock(return_value=db_pos)
    repo.record_trade_and_close_position = AsyncMock()
    repo.record_trade = AsyncMock()
    repo.close_position = AsyncMock()

    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[exchange_pos])
    exchange.place_order = AsyncMock(return_value=Order(
        id="oid-1", symbol=exchange_pos.symbol, side="sell",
        size=exchange_pos.size, order_type=OrderType.MARKET,
        filled_price=110.0, status=OrderStatus.FILLED,
    ))

    portfolio = MagicMock()
    portfolio.record_pnl = AsyncMock()

    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=None, control=MagicMock(),
    )
    runner.portfolio = portfolio
    return runner, repo


@pytest.mark.asyncio
async def test_flat_all_uses_db_entry_price(monkeypatch):
    """Exchange VWAP is 95 (averaging two legs); DB shows the strategy's
    actual entry was 100. PnL must be (110 - 100) × 1.0 = +10 minus fee,
    NOT (110 - 95) = +15. The exchange VWAP is wrong because another
    strategy added to the position at a different price."""
    from hypertrade.config import settings
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)

    exchange_pos = Position(symbol="BTC", side="long", size=1.0, entry_price=95.0)
    db_pos = MagicMock()
    db_pos.symbol = "BTC"
    db_pos.entry_price = 100.0
    db_pos.strategy_name = "strat_x"

    runner, repo = _runner_with(exchange_pos, db_pos)
    await runner._flat_all_positions()

    repo.record_trade_and_close_position.assert_awaited_once()
    call = repo.record_trade_and_close_position.await_args
    assert call.kwargs["pnl"] == pytest.approx(10.0)  # 110 - 100, not 110 - 95
    assert call.kwargs["strategy_name"] == "strat_x"  # uses real strategy name


@pytest.mark.asyncio
async def test_flat_all_atomic_call(monkeypatch):
    """Single atomic call instead of record_trade + close_position. No
    crash window between two writes."""
    from hypertrade.config import settings
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)

    exchange_pos = Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)
    db_pos = MagicMock()
    db_pos.symbol = "ETH"
    db_pos.entry_price = 2000.0
    db_pos.strategy_name = "ethstrat"

    runner, repo = _runner_with(exchange_pos, db_pos)
    runner.exchange.place_order = AsyncMock(return_value=Order(
        id="oid", symbol="ETH", side="sell", size=2.0,
        order_type=OrderType.MARKET, filled_price=2100.0,
        status=OrderStatus.FILLED,
    ))

    await runner._flat_all_positions()

    repo.record_trade_and_close_position.assert_awaited_once()
    # Old (non-atomic) APIs must NOT be called
    repo.record_trade.assert_not_called()
    repo.close_position.assert_not_called()


@pytest.mark.asyncio
async def test_flat_all_short_pnl_calc(monkeypatch):
    """Short side: PnL = (entry - exit) × size. With exchange VWAP wrong
    (entry 105) and DB entry 100, exit 90: real PnL = (100 - 90) × 1
    = +10, not (105 - 90) = +15."""
    from hypertrade.config import settings
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)

    exchange_pos = Position(symbol="SOL", side="short", size=1.0, entry_price=105.0)
    db_pos = MagicMock()
    db_pos.symbol = "SOL"
    db_pos.entry_price = 100.0
    db_pos.strategy_name = "solstrat"

    runner, repo = _runner_with(exchange_pos, db_pos)
    runner.exchange.place_order = AsyncMock(return_value=Order(
        id="oid", symbol="SOL", side="buy", size=1.0,
        order_type=OrderType.MARKET, filled_price=90.0,
        status=OrderStatus.FILLED,
    ))

    await runner._flat_all_positions()

    repo.record_trade_and_close_position.assert_awaited_once()
    call = repo.record_trade_and_close_position.await_args
    assert call.kwargs["pnl"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_flat_all_no_db_rec_falls_back_to_record_trade(monkeypatch):
    """If there's no open DB record (rare exchange-side orphan), still
    record the trade for history; don't try to close a non-existent row."""
    from hypertrade.config import settings
    monkeypatch.setattr(settings, "taker_fee_rate", 0.0)

    exchange_pos = Position(symbol="DOGE", side="long", size=10.0, entry_price=0.5)
    runner, repo = _runner_with(exchange_pos, db_pos=None)
    runner.exchange.place_order = AsyncMock(return_value=Order(
        id="oid", symbol="DOGE", side="sell", size=10.0,
        order_type=OrderType.MARKET, filled_price=0.6,
        status=OrderStatus.FILLED,
    ))

    await runner._flat_all_positions()

    repo.record_trade.assert_awaited_once()
    repo.record_trade_and_close_position.assert_not_called()
