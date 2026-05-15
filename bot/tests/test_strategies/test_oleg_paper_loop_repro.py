"""Reproduction test for the oleg_aryukov instant-stop-out loop.

See ``docs/investigations/oleg-paper-loop-2026-05-14.md`` for full
background. Marked ``xfail(strict=True)`` so this test:

  * Documents the bug shape in CI without breaking the build, AND
  * Will FAIL CI loudly the moment the bug is fixed
    (forcing whoever lands the fix to also flip this to a regular
    passing test in the same PR).

The bug: ``oleg_aryukov.on_candle`` evaluates trail-stop and SL-hit
checks against bar data that PRE-DATES the position entry. On the
very first tick after a fresh open, the trail extreme ratchets to the
prior bar's low and the SL-hit comparison fires on the prior bar's
high — instantly stopping out the trade.
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
@pytest.mark.xfail(
    strict=True,
    reason=(
        "oleg_aryukov instant-stop-out loop, 2026-05-14 paper bot. "
        "Trail-stop and SL-hit checks consult bar data that pre-dates "
        "the entry, so the very first tick after open instantly stops "
        "out. See docs/investigations/oleg-paper-loop-2026-05-14.md."
    ),
)
async def test_short_open_does_not_instant_stopout_from_pre_entry_bar() -> None:
    """Reproduces the loop's first half: a fresh short whose trail-extreme
    update + SL-hit check on a pre-entry bar fires CLOSE_SHORT immediately.

    Setup mirrors the prod 2026-05-14 numbers as closely as a synthetic
    test allows:

      * ``entry_price = 2286.10`` (from the prod log's
        ``entry $2,286.10``)
      * The closed bar consulted by the post-PR #127 ``df.iloc[-2]``
        carries ``low = 2245.10`` and ``high = 2287.80``, matching the
        ``SL hit at $2,267.96 (entry $2,286.10, high $2,287.80)``
        triplet:

          - trail_dist  = 2286.10 × 0.01 = 22.86
          - trail_stop  = 2245.10 + 22.86 = 2267.96
          - SL hit:    high (2287.80) >= sl (2267.96)  → True
    """
    entry = 2286.10
    strat = OlegAryukovStrategy(
        use_trailing=True,
        trailing_percent=1.0,
        stop_loss_percent=2.0,
    )
    # Simulate a freshly-opened short — restore_state mirrors the open
    # path's state mutation (sets entry / SL / trail extreme).
    strat.restore_state("short", entry)

    df = _flat_df(WARMUP + 5, price=entry)
    # Live partial bar is stripped by the runner before on_candle, so
    # tests see closed candles only — match that here. The "pre-entry"
    # bar that today's strategy reads via iloc[-2] is the SECOND-to-
    # last row; populate it with the prod-shape extremes.
    df.at[df.index[-2], "high"] = 2287.80
    df.at[df.index[-2], "low"] = 2245.10
    df.at[df.index[-2], "close"] = 2270.00

    result = await strat.on_candle(df)

    # CORRECT behaviour (asserts the bug is fixed): on the very first
    # tick after open, with no new closed bar SINCE the entry, the
    # manage-open branch should be a no-op.
    assert result is None, (
        "Fresh short instantly stopped out on a pre-entry bar — "
        "trail/SL-hit must not consult bars that closed before entry. "
        f"Got {result}"
    )
