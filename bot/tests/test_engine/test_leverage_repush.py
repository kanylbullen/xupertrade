"""Tests for per-tick leverage push (audit H1).

Pre-fix: HL leverage was set ONCE at startup. If the operator bumped a
strategy's `s.leverage` at runtime (Redis HSET, dashboard endpoint),
the bot's notional calc used the new value but HL still had the
startup leverage → margin used = 10× expected on a 10× bump →
liquidation path.

Post-fix: `_ensure_leverage_pushed` is called at the start of every
OPEN signal. It compares per-coin max(s.leverage) against the last
pushed value and re-pushes if changed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.runner import EngineRunner


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
    """update_leverage exception must not propagate — the open continues
    with whatever leverage HL currently has, and we log."""
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
