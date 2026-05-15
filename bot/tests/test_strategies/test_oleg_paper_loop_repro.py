"""Regression tests for the oleg_aryukov instant-stop-out loop.

See ``docs/investigations/oleg-paper-loop-2026-05-14.md`` for full
background.

The bug: ``oleg_aryukov.on_candle`` evaluated trail-stop and SL-hit
checks against bar data that PRE-DATED the position entry. On the
very first tick after a fresh open, the trail extreme ratcheted to the
prior bar's low and the SL-hit comparison fired on the prior bar's
high — instantly stopping out the trade.

The fix tracks ``_entry_bar_ts`` and skips the manage-open block on any
bar with ``timestamp <= _entry_bar_ts``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from hypertrade.engine.signals import SignalAction
from hypertrade.strategies.oleg_aryukov import OlegAryukovStrategy


def _ts_hourly(i: int) -> datetime:
    base = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(hours=i)


def _flat_df(n: int, price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [price] * n,
            "high": [price * 1.0005] * n,
            "low": [price * 0.9995] * n,
            "close": [price] * n,
            "volume": [1000.0] * n,
            "timestamp": [_ts_hourly(i) for i in range(n)],
        }
    )


# Warmup constant matches TestOlegAryukovStrategy.WARMUP in
# test_more_strategies.py — keeps the test self-contained without coupling.
WARMUP = max(20, 25 * 2, 52, 50, 200) + 20  # 220


@pytest.mark.asyncio
async def test_short_open_does_not_instant_stopout_from_pre_entry_bar() -> None:
    """A fresh short on bar N must not stop out from bar N's own data.

    Reproduces the loop's first half. Setup mirrors the prod 2026-05-14
    numbers as closely as a synthetic test allows:

      * ``entry_price = 2286.10`` (from the prod log's
        ``entry $2,286.10``)
      * The bar consulted by the buggy logic carries
        ``low = 2245.10`` and ``high = 2287.80``, which under the
        pre-fix manage-open block produced
        ``SL hit at $2,267.96 (entry $2,286.10, high $2,287.80)``.

    Post-fix: the entry bar's timestamp is stamped into ``_entry_bar_ts``
    so the manage-open block is a no-op until a strictly newer closed
    bar arrives.
    """
    entry = 2286.10
    strat = OlegAryukovStrategy(
        use_trailing=True,
        trailing_percent=1.0,
        stop_loss_percent=2.0,
    )
    df = _flat_df(WARMUP + 5, price=entry)
    # Place SL-triggering extremes on the LATEST bar (iloc[-1]) — the same
    # bar that ``_entry_bar_ts`` is stamped to and the only bar
    # ``on_candle`` actually inspects via ``df.iloc[-1]``. With the guard
    # in place the manage-open block no-ops on this bar; if the guard
    # were removed, the high/low here would re-trigger the
    # instant-stop-out loop and fail the test.
    df.at[df.index[-1], "high"] = 2287.80
    df.at[df.index[-1], "low"] = 2245.10
    df.at[df.index[-1], "close"] = 2270.00

    # Simulate the open path's effect on internal state — including
    # the new ``_entry_bar_ts`` stamp on the latest closed bar.
    strat.restore_state("short", entry)
    strat._entry_bar_ts = df.iloc[-1]["timestamp"]

    result = await strat.on_candle(df)

    assert result is None, (
        "Fresh short instantly stopped out on a pre-entry bar — "
        "trail/SL-hit must not consult bars that closed before entry. "
        f"Got {result}"
    )


@pytest.mark.asyncio
async def test_short_sl_hit_fires_on_bar_after_entry() -> None:
    """Once a strictly-newer bar arrives, SL/TP/trail logic fires normally.

    Bar N: enter short at 2286.10 (SL = 2331.82 = entry × 1.02).
    Bar N+1: high spikes to 2400 — well above SL — must trigger CLOSE_SHORT.
    """
    entry = 2286.10
    strat = OlegAryukovStrategy(
        use_trailing=False,
        stop_loss_percent=2.0,
    )
    # Build df where iloc[-2] is the entry bar and iloc[-1] is the post-entry
    # bar with the SL-breaching high.
    df = _flat_df(WARMUP + 5, price=entry)
    df.at[df.index[-1], "high"] = 2400.0  # > SL of 2331.82
    df.at[df.index[-1], "low"] = entry
    df.at[df.index[-1], "close"] = 2350.0

    strat.restore_state("short", entry)
    # Stamp the entry on the SECOND-to-last bar — so iloc[-1] is strictly newer.
    strat._entry_bar_ts = df.iloc[-2]["timestamp"]

    result = await strat.on_candle(df)

    assert result is not None, "SL hit on bar after entry must fire CLOSE_SHORT"
    assert result.action == SignalAction.CLOSE_SHORT
    assert "SL" in result.reason
