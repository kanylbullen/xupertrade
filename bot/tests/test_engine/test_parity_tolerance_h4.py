"""Tests for tightened parity tolerance (audit H4).

Pre-fix: tolerance = 10 × szDecimals-step. For SOL (szDecimals=2) that's
0.1 SOL ≈ $15 of drift slipping through silently — on a $200 position
that's a 7.5% silent error.

Post-fix: tolerance = min(10 × step, 0.5% × expected_size). The looser
bound never wins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.runner import EngineRunner


def _runner_with(db_positions, exchange_positions, sz_decimals=None):
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=db_positions)
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=exchange_positions)
    sz = sz_decimals or {"BTC": 5, "ETH": 4, "SOL": 2}
    exchange.get_size_precision = lambda s, sz=sz: sz.get(s, 4)
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=None, control=MagicMock(),
    )
    return runner


def _pos(symbol, side, size):
    p = MagicMock()
    p.symbol = symbol
    p.side = side
    p.size = size
    return p


@pytest.mark.asyncio
async def test_sol_small_position_drift_now_caught():
    """Pre-fix: SOL drift of 0.1 (= old step_bound) on a 1 SOL position
    was silently accepted (0.1 < 1.0 step, so OK). Post-fix: ratio_bound
    = 0.5% × 1.0 = 0.005, far smaller than the 0.1 drift → alert fires."""
    runner = _runner_with(
        db_positions=[_pos("SOL", "long", 1.0)],
        exchange_positions=[_pos("SOL", "long", 1.1)],
    )
    ok = await runner._check_parity_after_trade("SOL")
    assert ok is False, (
        "0.1 SOL drift on a 1 SOL position is 10% — must trip the alarm"
    )


@pytest.mark.asyncio
async def test_sol_tiny_drift_within_step_bound_still_ok():
    """A drift smaller than ONE szDecimals step (1e-2 for SOL) is
    exchange rounding noise — must NOT fire. The min() must not pull
    tolerance below the rounding floor."""
    runner = _runner_with(
        db_positions=[_pos("SOL", "long", 1.0)],
        exchange_positions=[_pos("SOL", "long", 1.005)],  # 0.005 = 0.5%
    )
    ok = await runner._check_parity_after_trade("SOL")
    assert ok is True, "0.5% drift should be at the boundary — within tolerance"


@pytest.mark.asyncio
async def test_btc_normal_step_tolerance_unchanged():
    """BTC szDecimals=5, step_bound = 1e-4. On a 0.01 BTC position
    ratio_bound = 5e-5 — tighter than step. min picks ratio. A 1e-4
    drift exceeds 5e-5 → must fire."""
    runner = _runner_with(
        db_positions=[_pos("BTC", "long", 0.01)],
        exchange_positions=[_pos("BTC", "long", 0.0101)],  # 0.0001 = 1%
    )
    ok = await runner._check_parity_after_trade("BTC")
    assert ok is False


@pytest.mark.asyncio
async def test_zero_position_uses_step_bound_only():
    """When db_net=ex_net=0, ratio_bound = 0 — would tolerate nothing.
    Fall back to step_bound so legitimate 'both flat' state passes."""
    runner = _runner_with(
        db_positions=[],
        exchange_positions=[],
    )
    ok = await runner._check_parity_after_trade("BTC")
    assert ok is True


@pytest.mark.asyncio
async def test_large_position_step_bound_wins():
    """For a 100 BTC position, ratio_bound = 0.5 BTC. step_bound = 1e-4
    is far tighter — min picks step. A drift of 1e-3 exceeds step → fires."""
    runner = _runner_with(
        db_positions=[_pos("BTC", "long", 100.0)],
        exchange_positions=[_pos("BTC", "long", 100.001)],
    )
    ok = await runner._check_parity_after_trade("BTC")
    assert ok is False
