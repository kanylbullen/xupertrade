"""Tests for per-tick leverage push (audit H1).

Pre-fix: HL leverage was set ONCE at startup. If the operator bumped a
strategy's `s.leverage` at runtime (Redis HSET, dashboard endpoint),
the bot's notional calc used the new value but HL still had the
startup leverage → margin used = 10× expected on a 10× bump →
liquidation path.

Post-fix: `_ensure_leverage_pushed` is called before every OPEN. It
compares per-coin max(s.leverage) against the last pushed value and
re-pushes if changed. Since 2026-09 a push that fails aborts the open
(it used to proceed at whatever leverage HL had).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction


def _make_runner(strategies):
    exchange = MagicMock()
    exchange.update_leverage = AsyncMock(return_value=True)
    runner = EngineRunner(
        exchange=exchange,
        strategies=strategies,
        repo=None,
        event_bus=None,
        control=None,
    )
    return runner, exchange


def _strat(name: str, symbol: str, leverage: int):
    s = MagicMock()
    s.name = name
    s.symbol = symbol
    s.leverage = leverage
    return s


@pytest.mark.asyncio
async def test_first_open_pushes_leverage():
    """No previous push tracked → must push on first OPEN."""
    runner, exchange = _make_runner([_strat("a", "BTC", 5)])
    await runner._ensure_leverage_pushed("BTC")
    exchange.update_leverage.assert_awaited_once_with("BTC", 5, is_cross=True)
    assert runner._pushed_leverage["BTC"] == 5


@pytest.mark.asyncio
async def test_no_change_skips_push():
    """When `_pushed_leverage[symbol]` already matches the target,
    don't call update_leverage (avoid wasteful HL traffic)."""
    runner, exchange = _make_runner([_strat("a", "BTC", 5)])
    runner._pushed_leverage["BTC"] = 5
    await runner._ensure_leverage_pushed("BTC")
    exchange.update_leverage.assert_not_called()


@pytest.mark.asyncio
async def test_bump_triggers_repush():
    """Operator bumps `s.leverage` from 5 to 10 — the next OPEN must
    push the new value before sending the order."""
    s = _strat("a", "BTC", 5)
    runner, exchange = _make_runner([s])
    runner._pushed_leverage["BTC"] = 5

    s.leverage = 10  # operator override
    await runner._ensure_leverage_pushed("BTC")
    exchange.update_leverage.assert_awaited_once_with("BTC", 10, is_cross=True)
    assert runner._pushed_leverage["BTC"] == 10


@pytest.mark.asyncio
async def test_per_coin_max_across_strategies():
    """Per-coin leverage on HL is a single value. The target is the
    max(s.leverage) across all strategies trading that coin."""
    runner, exchange = _make_runner([
        _strat("a", "ETH", 3),
        _strat("b", "ETH", 8),
        _strat("c", "ETH", 2),
        _strat("d", "BTC", 5),
    ])
    await runner._ensure_leverage_pushed("ETH")
    exchange.update_leverage.assert_awaited_once_with("ETH", 8, is_cross=True)


@pytest.mark.asyncio
async def test_unknown_symbol_defaults_to_1x():
    """A symbol with no strategy returns 1x as the safe default."""
    runner, exchange = _make_runner([_strat("a", "BTC", 5)])
    await runner._ensure_leverage_pushed("DOGE")
    exchange.update_leverage.assert_awaited_once_with("DOGE", 1, is_cross=True)


@pytest.mark.asyncio
async def test_exchange_failure_does_not_raise():
    """update_leverage exception must not propagate — the push reports
    failure (the caller aborts the open, see below) and we log."""
    runner, exchange = _make_runner([_strat("a", "BTC", 5)])
    exchange.update_leverage = AsyncMock(side_effect=RuntimeError("HL down"))
    # Must not raise
    await runner._ensure_leverage_pushed("BTC")
    # Failure path must NOT cache target as pushed (so we retry next OPEN)
    assert "BTC" not in runner._pushed_leverage


@pytest.mark.asyncio
async def test_exchange_rejection_does_not_cache():
    """If update_leverage returns False (HL rejected), don't cache —
    retry on the next OPEN."""
    runner, exchange = _make_runner([_strat("a", "BTC", 5)])
    exchange.update_leverage = AsyncMock(return_value=False)
    await runner._ensure_leverage_pushed("BTC")
    assert "BTC" not in runner._pushed_leverage


# ----------------------------------------------------------------------
# A failed push aborts the open (analysis-2026-09-15.md § 4)
# ----------------------------------------------------------------------


def _open_runner(update_leverage, *, pushed=None):
    s = _strat("kalman_breakout", "ETH", 5)
    repo = MagicMock()
    repo.get_open_position = AsyncMock(return_value=None)
    repo.get_open_positions = AsyncMock(return_value=[])
    control = MagicMock()
    control.get_allow_multi_coin = AsyncMock(return_value=False)
    exchange = MagicMock()
    exchange.update_leverage = update_leverage
    exchange.place_order = AsyncMock()
    bus = MagicMock()
    bus.publish = AsyncMock()
    runner = EngineRunner(
        exchange=exchange, strategies=[s], repo=repo,
        event_bus=bus, control=control, pushed_leverage=pushed,
    )
    runner.portfolio = MagicMock()
    runner.portfolio.check_risk_limits = AsyncMock(return_value=True)
    return runner, exchange, bus


def _eth_long():
    return Signal(
        action=SignalAction.OPEN_LONG, symbol="ETH", strategy_name="kalman_breakout",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "raised"])
async def test_failed_push_aborts_the_open(monkeypatch, failure):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    push = (
        AsyncMock(return_value=False) if failure == "rejected"
        else AsyncMock(side_effect=RuntimeError("HL down"))
    )
    runner, exchange, bus = _open_runner(push)

    ok = await runner._execute_signal(_eth_long(), current_price=2000.0, leverage=5)

    assert ok is False
    exchange.place_order.assert_not_awaited()
    events = [c.args[0] for c in bus.publish.await_args_list]
    assert [e.strategy for e in events] == ["leverage/ETH"]


@pytest.mark.asyncio
async def test_failed_push_publishes_once_per_target(monkeypatch):
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    runner, exchange, bus = _open_runner(AsyncMock(return_value=False))

    for _ in range(3):
        await runner._execute_signal(_eth_long(), current_price=2000.0, leverage=5)

    assert bus.publish.await_count == 1
    assert exchange.update_leverage.await_count == 3, "every OPEN retries the push"
    exchange.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_after_failure_rearms_the_alert():
    push = AsyncMock(side_effect=[False, True, False])
    runner, exchange, bus = _open_runner(push)

    assert await runner._ensure_leverage_pushed("ETH") is False
    assert await runner._ensure_leverage_pushed("ETH") is True
    runner._pushed_leverage.clear()  # force a new push to the same target
    assert await runner._ensure_leverage_pushed("ETH") is False

    assert bus.publish.await_count == 2


@pytest.mark.asyncio
async def test_boot_push_seeds_last_accepted():
    """main.py hands the runner what the boot-time push got accepted: an
    OPEN at that leverage needs no push, so an HL blip cannot abort it."""
    runner, exchange, bus = _open_runner(
        AsyncMock(return_value=False), pushed={"ETH": 5},
    )

    assert await runner._ensure_leverage_pushed("ETH") is True
    exchange.update_leverage.assert_not_awaited()
