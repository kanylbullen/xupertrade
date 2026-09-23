"""qullamagi_breakout counts BARS, not calls.

The runner calls on_candle every 60 s with the same closed bar until the
next one closes. `_bars_since_flat` and `_bars_in_trade` were `+= 1` per
call, so the 3-bar re-entry cooldown on 1h candles lasted about three
minutes and `_bars_in_trade` ran 60× fast — the class of the 2026-05-09
hash_momentum incident. Pine (`tv-source/qullamagi_breakout.pine`) counts
`bar_index - lastFlatBar > cooldownBars` and `barsInTrade += 1` per bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from hypertrade.strategies.qullamagi_breakout import QullamagiBreakoutStrategy

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
N = 210  # warmup is max(len200, pullback_lookback, 50) + 5 = 205


def _frame(n_bars: int, price: float = 100.0) -> pd.DataFrame:
    """Flat hourly candles; the last one closes at T0 + (n_bars - 1) h."""
    return pd.DataFrame({
        "timestamp": [T0 + timedelta(hours=i) for i in range(n_bars)],
        "open": [price] * n_bars,
        "high": [price * 1.001] * n_bars,
        "low": [price * 0.999] * n_bars,
        "close": [price] * n_bars,
        "volume": [1000.0] * n_bars,
    })


async def _tick(strat, frame, times: int = 1) -> None:
    for _ in range(times):
        await strat.on_candle(frame)


@pytest.mark.asyncio
async def test_cooldown_counts_bars_not_ticks():
    strat = QullamagiBreakoutStrategy()
    await _tick(strat, _frame(N))       # baseline on the exit bar
    strat._reset()                      # its own exit: cooldown starts

    await _tick(strat, _frame(N), times=60)   # an hour of 60 s ticks
    assert strat._bars_since_flat == 0

    await _tick(strat, _frame(N + 1), times=60)
    assert strat._bars_since_flat == 1

    await _tick(strat, _frame(N + 3), times=5)
    assert strat._bars_since_flat == 3                   # still blocked
    assert strat._bars_since_flat <= strat.cooldown_bars

    await _tick(strat, _frame(N + 4))
    assert strat._bars_since_flat == 4                   # Pine: 4 > 3, free


@pytest.mark.asyncio
async def test_bars_in_trade_counts_bars_not_ticks():
    strat = QullamagiBreakoutStrategy()
    await _tick(strat, _frame(N))
    strat.restore_state("long", 100.0)

    await _tick(strat, _frame(N), times=60)
    assert strat.holds_position()
    assert strat._bars_in_trade == 0

    await _tick(strat, _frame(N + 2), times=10)
    assert strat._bars_in_trade == 2


@pytest.mark.asyncio
async def test_a_restored_counter_adds_the_bars_elapsed_since_the_snapshot():
    """The snapshot is written at the close; the first candle after a
    restart ten bars later must see the cooldown as long over, not
    re-impose it."""
    closed = QullamagiBreakoutStrategy()
    await _tick(closed, _frame(N))
    closed.restore_state("long", 100.0)
    closed._reset()
    snapshot = closed.export_state()
    assert snapshot["bars_since_flat"] == 0
    assert snapshot["position_side"] is None

    fresh = QullamagiBreakoutStrategy()
    assert fresh.restore_cooldown_only(snapshot) is False
    assert fresh._bars_since_flat == 0

    await _tick(fresh, _frame(N + 10))
    assert fresh._bars_since_flat == 10
    assert fresh.export_state() is None  # cooldown over: snapshot not needed


def test_frames_without_timestamps_keep_one_step_per_call():
    """Backtest-style frames without a timestamp column: unchanged."""
    import asyncio

    strat = QullamagiBreakoutStrategy()
    strat._reset()
    frame = _frame(N).drop(columns=["timestamp"])
    asyncio.run(_tick(strat, frame, times=3))
    assert strat._bars_since_flat == 3
