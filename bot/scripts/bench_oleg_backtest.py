"""Benchmark / reproducibility script for the oleg_aryukov backtest work.

Companion to docs/plans/oleg-aryukov-backtest-perf.md. Runs:

1. Per-call indicator cost at several growing-window sizes (the backtest
   runner replays candles with df.iloc[: i + 1] per bar, so per-call cost
   at history length k is what accumulates).
2. A full synthetic 4,320-bar (180d of 1h) replay through the real
   hypertrade.backtest.run_backtest — no network, no DB.

Usage:
    cd bot && uv run python scripts/bench_oleg_backtest.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Allow `uv run python scripts/bench_oleg_backtest.py` from bot/: the
# script's own directory would otherwise be sys.path[0], not bot/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypertrade.backtest.runner import run_backtest  # noqa: E402
from hypertrade.strategies.oleg_aryukov import (  # noqa: E402
    _nadaraya_watson_series,
    _rci,
)

N_BARS = 4320  # 180 days * 24h
WARMUP = 250


def make_candles(n: int, seed: int = 7, s0: float = 2500.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = s0 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.0015, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.0015, n)))
    open_ = np.concatenate([[s0], close[:-1]])
    ts = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.abs(rng.normal(1000, 100, n)),
            "timestamp": ts,
        }
    )


def main() -> None:
    candles = make_candles(N_BARS)

    print(f"Per-call cost at growing window sizes (bars):")
    for name, fn in (
        ("_rci(L=9)", lambda k: _rci(candles["close"].iloc[:k], 9)),
        ("_rci(L=26)", lambda k: _rci(candles["close"].iloc[:k], 26)),
        ("_rci(L=52)", lambda k: _rci(candles["close"].iloc[:k], 52)),
        (
            "_nadaraya_watson_series",
            lambda k: _nadaraya_watson_series(
                candles["close"].iloc[:k].values, 3.0, 50
            ),
        ),
    ):
        row = []
        for k in (1000, 2000, 4000):
            t0 = time.perf_counter()
            fn(k)
            row.append(f"k={k}: {(time.perf_counter() - t0) * 1000:.1f}ms")
        print(f"  {name:<24} " + "  ".join(row))

    async def run() -> None:
        from hypertrade.strategies.oleg_aryukov import OlegAryukovStrategy

        strat = OlegAryukovStrategy()
        t0 = time.perf_counter()
        res = await run_backtest(strat, candles, warmup_bars=WARMUP)
        dt = time.perf_counter() - t0
        print(f"\nrun_backtest {N_BARS} bars: {dt:.1f}s")
        print(
            f"  trades={res.num_trades} round_trips={res.num_round_trips} "
            f"return={res.total_return_pct * 100:+.2f}% fees=${res.fees_paid:.2f}"
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
