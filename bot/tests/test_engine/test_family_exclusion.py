"""Family-level allow_multi_coin=False exclusion (correlation grouping).

Backlog "Correlation grouping": cdc_macd and macd_zero are mathematically
near-identical (EMA12/26 cross ≡ MACD zero-cross) and can end up taking
effectively the same trade. When the allow_multi_coin Redis flag is False
the engine must therefore refuse an OPEN not only when another strategy
holds the SAME coin (legacy per-coin rule) but also when another strategy
of the same `family` holds a position on ANY coin — the two entries would
be effectively the same trade in duplicate.

The runner resolves families through the strategy registry, so these tests
import the strategy modules needed for registration rather than wiring
strategy instances into the runner.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# Importing registers the classes in the global strategy registry. The
# family gate looks strategies up by signal.strategy_name, NOT via
# runner.strategies (which is empty here).
import hypertrade.strategies.cdc_macd  # noqa: F401
import hypertrade.strategies.macd_zero  # noqa: F401
import hypertrade.strategies.supertrend  # noqa: F401
from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Order, OrderStatus, OrderType
from hypertrade.strategies.registry import get_strategy_family


def _db_pos(
    strategy: str,
    symbol: str,
    side: str = "long",
    size: float = 0.01,
    entry: float = 100.0,
) -> MagicMock:
    p = MagicMock()
    p.strategy_name = strategy
    p.symbol = symbol
    p.side = side
    p.size = size
    p.entry_price = entry
    p.leverage = 1
    return p


def _runner(open_positions: list) -> tuple[EngineRunner, MagicMock]:
    """Build a runner with stubbed deps, allow_multi_coin pinned False."""
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=open_positions)
    repo.get_open_position = AsyncMock(return_value=None)
    repo.get_open_position_any = AsyncMock(return_value=None)

    portfolio = MagicMock()
    portfolio.check_risk_limits = AsyncMock(return_value=True)

    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=False)

    exchange = MagicMock()
    # AsyncMock so assert_not_awaited/assert_awaited work in the gate tests;
    # _allow_open_path() re-stubs it with a filled Order when needed.
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


def _allow_open_path(runner: EngineRunner) -> None:
    """Stub the post-gate fill path so an OPEN that passes all gates runs
    through to place_order — mirroring test_exposure_cap.test_under_cap_allows."""
    runner.exchange.place_order = AsyncMock(
        return_value=Order(
            id="x", symbol="SOL", side="buy", size=0.05,
            order_type=OrderType.MARKET, filled_price=2000.0,
            status=OrderStatus.FILLED,
        )
    )
    runner._record_trade_and_position = AsyncMock(return_value=None)
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    runner.repo.record_trade_and_open_position = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_same_family_on_other_coin_blocks_open(monkeypatch):
    """macd_zero holds BTC; cdc_macd (same macd_zero_cross family) must not
    open SOL. The legacy per-coin rule alone would allow this — the family
    rule is what catches the duplicated trade."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)  # isolate the gate
    held = _db_pos("macd_zero", "BTC")
    runner, _ = _runner([held])

    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="SOL", strategy_name="cdc_macd"
    )
    ok = await runner._execute_signal(sig, current_price=100.0)
    assert ok is False, "same-family position on another coin must block the open"
    runner.exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_family_on_same_coin_blocks_open(monkeypatch):
    """Same coin + same family: the legacy per-coin rule already blocks; the
    family rule must not loosen that."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    held = _db_pos("macd_zero", "SOL")
    runner, _ = _runner([held])
    # Legacy limit-1 coin query sees the row.
    runner.repo.get_open_position_any = AsyncMock(return_value=held)

    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="SOL", strategy_name="cdc_macd"
    )
    ok = await runner._execute_signal(sig, current_price=100.0)
    assert ok is False


@pytest.mark.asyncio
async def test_different_family_on_other_coin_allows_open(monkeypatch):
    """supertrend (family `supertrend`) holds BTC; cdc_macd opening SOL is a
    genuinely different signal — the family gate must not block it."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    held = _db_pos("supertrend", "BTC")
    runner, _ = _runner([held])
    _allow_open_path(runner)

    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="SOL", strategy_name="cdc_macd"
    )
    await runner._execute_signal(sig, current_price=100.0)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_allow_multi_true_disables_family_rule(monkeypatch):
    """The family exclusion only applies while allow_multi_coin is False —
    with the flag on, same-family positions on different coins coexist."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    held = _db_pos("macd_zero", "BTC")
    runner, _ = _runner([held])
    runner.control.get_allow_multi_coin = AsyncMock(return_value=True)
    _allow_open_path(runner)

    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="SOL", strategy_name="cdc_macd"
    )
    await runner._execute_signal(sig, current_price=100.0)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_own_position_does_not_trigger_family_rule(monkeypatch):
    """A strategy's own open row must not count as a family conflict
    (guards the name-equality filter in the gate). Synthetic setup: a
    strategy never trades two symbols live, but the gate must be robust
    to the row layout regardless."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    own = _db_pos("macd_zero", "BTC")
    runner, _ = _runner([own])
    _allow_open_path(runner)

    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="SOL", strategy_name="macd_zero"
    )
    await runner._execute_signal(sig, current_price=100.0)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_unknown_strategy_keeps_legacy_behaviour(monkeypatch):
    """A signal whose strategy_name is not in the registry has no family —
    only the legacy per-coin rule applies (same coin blocks, other coin
    doesn't)."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    held = _db_pos("macd_zero", "BTC")
    runner, _ = _runner([held])
    _allow_open_path(runner)

    assert get_strategy_family("ghost_strategy") is None
    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="SOL", strategy_name="ghost_strategy"
    )
    await runner._execute_signal(sig, current_price=100.0)
    runner.exchange.place_order.assert_awaited()


@pytest.mark.asyncio
async def test_family_rule_ignores_other_family_rows(monkeypatch):
    """Rows from several unrelated families must not block each other:
    supertrend + rsi_momentum hold BTC/SOL; keltner_breakout (keltner_channel
    family, nothing held) opening ETH passes the gate."""
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    import hypertrade.strategies.keltner_breakout  # noqa: F401

    held = [
        _db_pos("supertrend", "BTC"),
        _db_pos("rsi_momentum", "SOL"),
    ]
    runner, _ = _runner(held)
    _allow_open_path(runner)

    sig = Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH", strategy_name="keltner_breakout"
    )
    await runner._execute_signal(sig, current_price=100.0)
    runner.exchange.place_order.assert_awaited()