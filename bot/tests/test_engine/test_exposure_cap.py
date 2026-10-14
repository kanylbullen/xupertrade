"""Tests for the MAX_TOTAL_EXPOSURE_USD cap (audit C1, units fixed
2026-09).

Audit C1: the cap used to count each open row as `MAX_POSITION_SIZE_USD`
regardless of its actual size, making it a position-COUNT cap. It then
became a "margin" sum — but `PositionRecord` has no leverage column, so
`getattr(p, "leverage", 1)` always read 1: open rows counted at full
notional while the new order counted notional / leverage, two units in
one sum (`bot/reports/analysis-2026-09-15.md` § 4). The cap is now
NOTIONAL on both sides: Σ open `size × entry_price` + the new order's
`size × price`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Order, OrderStatus, OrderType


def _db_pos(symbol: str, side: str, size: float, entry: float, leverage: int = 1) -> MagicMock:
    p = MagicMock()
    p.symbol = symbol
    p.side = side
    p.size = size
    p.entry_price = entry
    # Real PositionRecord rows have no leverage column; the mock keeps one
    # only to prove the cap no longer reads it.
    p.leverage = leverage
    p.strategy_name = f"strat_{symbol}"
    return p


def _runner(open_positions: list) -> tuple:
    """Build a runner with stubbed deps. Returns (runner, repo)."""
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=open_positions)
    repo.get_open_position = AsyncMock(return_value=None)

    portfolio = MagicMock()
    portfolio.check_risk_limits = AsyncMock(return_value=True)

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=True)

    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)
    exchange.place_order = AsyncMock()

    runner = EngineRunner(
        exchange=exchange,
        strategies=[],
        repo=repo,
        event_bus=None,
        control=control,
    )
    runner.portfolio = portfolio
    return runner, repo


def _allow_fill(runner: EngineRunner, size: float, price: float) -> None:
    runner.exchange.place_order = AsyncMock(
        return_value=Order(
            id="x", symbol="ETH", side="buy", size=size,
            order_type=OrderType.MARKET, filled_price=price,
            status=OrderStatus.FILLED,
        )
    )
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    runner.repo.record_trade_and_open_position = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_open_rows_count_at_full_notional(monkeypatch):
    """4 open rows of $1,000 notional each = $4,000, whatever leverage
    they were opened at. A new $200 position against a $4,100 cap must
    block ($4,200 > $4,100). The pre-C1 count cap (4 × $200 = $800) and a
    margin sum (4 × $100 at 10x = $400) would both have let it through."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 4_100)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [
        _db_pos(f"COIN{i}", "long", 10.0, 100.0, leverage=10) for i in range(4)
    ]
    runner, _ = _runner(open_pos)

    sig = Signal(action=SignalAction.OPEN_LONG, symbol="NEW", strategy_name="new_strat")
    ok = await runner._execute_signal(sig, current_price=50.0, leverage=1)
    assert ok is False, "$4,000 open notional + $200 new > $4,100 cap"
    runner.exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_size_override_counted_at_real_notional(monkeypatch):
    """vvv_hedge emits Signal(size=400). At VVV ≈ $5 that's $2k notional —
    counted at $2k, not as MAX_POSITION_SIZE_USD."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 1000)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    runner, _ = _runner(open_positions=[])

    sig = Signal(
        action=SignalAction.OPEN_LONG,
        symbol="VVV",
        strategy_name="vvv_hedge",
        size=400.0,
    )
    ok = await runner._execute_signal(sig, current_price=5.0, leverage=1)
    assert ok is False, "should block: $2000 vvv_hedge notional > $1000 cap"


@pytest.mark.asyncio
async def test_under_cap_allows(monkeypatch):
    """Sanity: a small position well under the cap passes the gate."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 5000)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [_db_pos("BTC", "long", 0.01, 50_000.0, leverage=1)]  # $500
    runner, _ = _runner(open_pos)
    _allow_fill(runner, size=0.1, price=2000.0)

    sig = Signal(action=SignalAction.OPEN_LONG, symbol="ETH", strategy_name="ethstrat")
    # $500 open + $200 new = $700 ≤ $5000 cap → the order goes out.
    await runner._execute_signal(sig, current_price=2000.0, leverage=1)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_leverage_5_counts_new_order_at_notional(monkeypatch):
    """The unit bug. At 5x, MAX_POSITION_SIZE_USD $200 buys $1,000 of
    notional. With $500 already open the projected total is $1,500:
    over a $1,499 cap. The old code divided only the NEW side by
    leverage ($500 + $200 = $700) and let it through."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 1_499)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [_db_pos("BTC", "long", 0.01, 50_000.0)]  # $500 notional
    runner, _ = _runner(open_pos)

    sig = Signal(action=SignalAction.OPEN_LONG, symbol="ETH", strategy_name="ethstrat")
    ok = await runner._execute_signal(sig, current_price=2000.0, leverage=5)
    assert ok is False, "$500 + $1,000 (5x notional) > $1,499 cap"
    runner.exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_leverage_5_at_exactly_the_cap_passes(monkeypatch):
    """Boundary: $500 + $1,000 = $1,500 against a $1,500 cap is allowed
    (the cap blocks only when crossed)."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 1_500)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [_db_pos("BTC", "long", 0.01, 50_000.0)]
    runner, _ = _runner(open_pos)
    _allow_fill(runner, size=0.5, price=2000.0)

    sig = Signal(action=SignalAction.OPEN_LONG, symbol="ETH", strategy_name="ethstrat")
    await runner._execute_signal(sig, current_price=2000.0, leverage=5)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_open_row_leverage_is_not_divided(monkeypatch):
    """An open row counts at size × entry even when opened at 5x — a
    leverage attribute on the row object is ignored. $5,000 open (0.1 BTC
    @ 50k) + $200 new > $5,000 cap."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 5_000)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [_db_pos("BTC", "long", 0.1, 50_000.0, leverage=5)]
    runner, _ = _runner(open_pos)

    sig = Signal(action=SignalAction.OPEN_LONG, symbol="ETH", strategy_name="ethstrat")
    ok = await runner._execute_signal(sig, current_price=2000.0, leverage=1)
    assert ok is False
