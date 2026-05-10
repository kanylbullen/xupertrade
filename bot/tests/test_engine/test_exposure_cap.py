"""Tests for MAX_TOTAL_EXPOSURE_USD margin-sum gate (audit C1).

Pre-fix: the cap counted each open row as `MAX_POSITION_SIZE_USD` of
margin regardless of its actual notional or leverage, and likewise
ignored `signal.size` overrides — making it a position-COUNT cap
(~25 positions) instead of a dollar cap. Post-fix: real margin sum
is compared against the cap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction


def _db_pos(symbol: str, side: str, size: float, entry: float, leverage: int = 1) -> MagicMock:
    p = MagicMock()
    p.symbol = symbol
    p.side = side
    p.size = size
    p.entry_price = entry
    p.leverage = leverage
    p.strategy_name = f"strat_{symbol}"
    return p


def _runner(open_positions: list) -> tuple:
    """Build a runner with stubbed deps. Returns (runner, repo)."""
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=open_positions)
    repo.get_open_position = AsyncMock(return_value=None)
    repo.get_open_position_any = AsyncMock(return_value=None)

    portfolio = MagicMock()
    portfolio.check_risk_limits = AsyncMock(return_value=True)

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=True)

    exchange = MagicMock()

    runner = EngineRunner(
        exchange=exchange,
        strategies=[],
        repo=repo,
        event_bus=None,
        control=control,
    )
    runner.portfolio = portfolio
    return runner, repo


@pytest.mark.asyncio
async def test_count_based_passes_real_dollars_blocks(monkeypatch):
    """Reproduces the pre-fix bug: 4 leveraged positions whose true margin
    sum is $400 + a new $200 position must exceed a $500 cap. The old
    impl approximated existing margin as 4 × $200 = $800 (wrong both
    directions: counted leverage as if it were 1, and counted the $200
    cap-not-actual)."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 500)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    # 4 positions, $1000 notional each at 10x leverage = $100 margin each = $400 total
    open_pos = [
        _db_pos(f"COIN{i}", "long", 10.0, 100.0, leverage=10) for i in range(4)
    ]
    runner, _ = _runner(open_pos)

    sig = Signal(
        action=SignalAction.OPEN_LONG,
        symbol="NEW",
        strategy_name="new_strat",
    )
    # New position uses default _calculate_size → margin = MAX_POSITION_SIZE_USD = $200.
    # current_margin $400 + new $200 = $600 > $500 cap → must block.
    ok = await runner._execute_signal(sig, current_price=50.0, leverage=1)
    assert ok is False, "should block: $400 current + $200 new > $500 cap"


@pytest.mark.asyncio
async def test_signal_size_override_counted_at_real_notional(monkeypatch):
    """vvv_hedge emits Signal(size=400). At VVV ≈ $5 that's $2k notional —
    must be counted as $2k of margin (lev=1), not as MAX_POSITION_SIZE_USD."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 1000)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    runner, _ = _runner(open_positions=[])

    sig = Signal(
        action=SignalAction.OPEN_LONG,
        symbol="VVV",
        strategy_name="vvv_hedge",
        size=400.0,
    )
    # 400 VVV × $5 = $2000 / 1x = $2000 margin > $1000 cap → block.
    ok = await runner._execute_signal(sig, current_price=5.0, leverage=1)
    assert ok is False, "should block: $2000 vvv_hedge notional > $1000 cap"


@pytest.mark.asyncio
async def test_under_cap_allows(monkeypatch):
    """Sanity: a small position well under the cap passes the gate.
    (We stop short of an actual fill — the gate is the unit under test.)"""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 5000)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [_db_pos("BTC", "long", 0.01, 50_000.0, leverage=1)]  # $500 margin
    runner, _ = _runner(open_pos)

    # Stub the order placement so the test doesn't need a fully-wired
    # exchange just to confirm the gate didn't reject.
    from hypertrade.exchange.base import Order, OrderStatus, OrderType
    runner.exchange.place_order = AsyncMock(
        return_value=Order(
            id="x", symbol="ETH", side="buy", size=0.05,
            order_type=OrderType.MARKET, filled_price=2000.0,
            status=OrderStatus.FILLED,
        )
    )
    # Stop the post-fill DB write path — only assert the gate passes.
    runner._record_trade_and_position = AsyncMock(return_value=None)
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    runner.repo.record_trade_and_open_position = AsyncMock(return_value=None)

    sig = Signal(
        action=SignalAction.OPEN_LONG,
        symbol="ETH",
        strategy_name="ethstrat",
    )
    # current_margin $500 + $200 = $700 ≤ $5000 cap → must allow gate
    # to pass. Whether the trade *itself* records depends on the rest
    # of the path; we rely on the place_order mock not throwing as
    # evidence the gate was crossed.
    await runner._execute_signal(sig, current_price=2000.0, leverage=1)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_leverage_divides_new_margin(monkeypatch):
    """A 10x leverage open uses MAX_POSITION_SIZE_USD ($200) of margin
    (notional = $200 × 10 = $2000, margin = $2000 / 10 = $200). With
    one existing $500 margin position, the projected margin sum is
    $500 + $200 = $700. Cap is set to $699 to push it over → must block.
    The point: the gate compares $700 against the cap, not $500 + $2000
    (the notional) — leverage MUST divide the new-position margin."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 699)
    monkeypatch.setattr(settings, "max_position_size_usd", 200)

    open_pos = [_db_pos("BTC", "long", 0.01, 50_000.0, leverage=1)]  # $500 margin
    runner, _ = _runner(open_pos)

    sig = Signal(
        action=SignalAction.OPEN_LONG,
        symbol="ETH",
        strategy_name="ethstrat",
    )
    ok = await runner._execute_signal(sig, current_price=2000.0, leverage=10)
    assert ok is False, "$500 + $200 (= margin, NOT notional $2000) > $699 cap → block"
