"""Compare ATH-breakout overlay vs HODL vs DCA over the same window.

Question: with $10k to deploy on BTC, is it better to (a) lump-sum hold,
(b) DCA monthly, or (c) sit in cash and only deploy at new N-day highs
with a trailing exit?

For each ATH variant we report:
  - APR + Sharpe + max DD (the bot's standard backtest metrics)
  - time-in-market %  (fraction of daily bars holding a position)
  - end-equity vs equivalent HODL deploy

Run from bot/:
  uv run python scripts/ath_vs_hold_dca.py --days 1825
"""

from __future__ import annotations

import argparse
import asyncio
import math

import pandas as pd

from hypertrade.backtest.runner import run_backtest
from hypertrade.data.feed import fetch_candles
from hypertrade.strategies.ath_breakout import AthBreakoutStrategy


def hodl_apr(initial: float, final: float, days: int) -> float:
    if initial <= 0 or days <= 0:
        return 0.0
    return (final / initial) ** (365.0 / days) - 1.0


def hodl_metrics(candles: pd.DataFrame, deploy_usd: float) -> dict:
    first = float(candles["close"].iloc[0])
    last = float(candles["close"].iloc[-1])
    btc = deploy_usd / first
    final = btc * last
    days = (
        candles["timestamp"].iloc[-1] - candles["timestamp"].iloc[0]
    ).days or 1
    # Daily returns for Sharpe
    rets = candles["close"].pct_change().dropna()
    sharpe = (rets.mean() / rets.std()) * math.sqrt(365) if rets.std() > 0 else 0.0
    # Max drawdown
    eq = candles["close"].astype(float)
    peak = eq.cummax()
    dd = (eq / peak - 1).min()
    return {
        "name": "HODL",
        "final": final,
        "ret_pct": (final / deploy_usd - 1) * 100,
        "apr_pct": hodl_apr(deploy_usd, final, days) * 100,
        "sharpe": float(sharpe),
        "max_dd_pct": abs(float(dd)) * 100,
        "trades": 1,
        "time_in_mkt_pct": 100.0,
    }


def dca_metrics(candles: pd.DataFrame, deploy_usd: float) -> dict:
    """Equal-slice DCA over the window. Splits deploy_usd into ~one slice
    per month, buying at the close of the first bar of each month."""
    df = candles.copy()
    # Group by (year, month) instead of pandas Period so tz-aware
    # timestamps from the candle feed don't trigger a timezone-drop
    # warning on `.dt.to_period("M")`.
    ts = pd.to_datetime(df["timestamp"])
    df["_ym"] = ts.dt.year * 12 + ts.dt.month
    first_bars = df.groupby("_ym").head(1)
    n_slices = len(first_bars)
    if n_slices == 0:
        raise ValueError("DCA: empty candle window")
    slice_usd = deploy_usd / n_slices

    btc = 0.0
    for _, row in first_bars.iterrows():
        btc += slice_usd / float(row["close"])

    last = float(candles["close"].iloc[-1])
    final = btc * last
    days = (
        candles["timestamp"].iloc[-1] - candles["timestamp"].iloc[0]
    ).days or 1
    return {
        "name": f"DCA ({n_slices} monthly slices)",
        "final": final,
        "ret_pct": (final / deploy_usd - 1) * 100,
        "apr_pct": hodl_apr(deploy_usd, final, days) * 100,
        "sharpe": float("nan"),  # not meaningful for accumulating position
        "max_dd_pct": float("nan"),
        "trades": n_slices,
        "time_in_mkt_pct": 100.0,  # DCA is always accumulating
    }


async def ath_metrics(
    candles: pd.DataFrame, deploy_usd: float, lookback: int, trail_pct: float
) -> dict:
    strat = AthBreakoutStrategy(lookback=lookback, trail_pct=trail_pct)
    res = await run_backtest(
        strategy=strat,
        candles=candles,
        initial_equity=deploy_usd,
        position_size_usd=deploy_usd,  # all-in on each entry
        fee_rate=0.00045,               # HL taker default
        slippage_bps=2.0,
    )
    # Time in market = sum(seconds in each round trip) / total seconds
    in_mkt_seconds = 0.0
    entry_ts = None
    for t in res.trades:
        if t.side in ("buy", "long") and entry_ts is None:
            entry_ts = t.timestamp
        elif t.pnl is not None and entry_ts is not None:
            in_mkt_seconds += (t.timestamp - entry_ts).total_seconds()
            entry_ts = None
    total_seconds = (res.end - res.start).total_seconds() or 1.0
    tim_pct = (in_mkt_seconds / total_seconds) * 100.0
    return {
        "name": f"ATH lb={lookback} tr={trail_pct:.0%}",
        "final": res.final_equity,
        "ret_pct": res.total_return_pct * 100,
        "apr_pct": res.apr * 100,
        "sharpe": res.sharpe,
        "max_dd_pct": res.max_drawdown_pct * 100,
        "trades": res.num_round_trips,
        "time_in_mkt_pct": tim_pct,
    }


def fmt_row(r: dict) -> str:
    final = f"${r['final']:,.0f}"
    ret = f"{r['ret_pct']:+.1f}%"
    apr = f"{r['apr_pct']:+.2f}%"
    sharpe = f"{r['sharpe']:.2f}" if not math.isnan(r['sharpe']) else "—"
    dd = f"{r['max_dd_pct']:.1f}%" if not math.isnan(r['max_dd_pct']) else "—"
    tim = f"{r['time_in_mkt_pct']:.0f}%" if not math.isnan(r['time_in_mkt_pct']) else "—"
    return (
        f"{r['name']:<28} {final:>10} {ret:>10} {apr:>9} "
        f"{sharpe:>7} {dd:>8} {r['trades']:>5} {tim:>6}"
    )


async def main(days: int) -> None:
    print(f"Fetching BTC 1d ({days}d)...")
    candles = await fetch_candles("BTC", "1d", limit=days)
    if candles is None or candles.empty:
        # The feed returns an empty DataFrame on HTTP/network failures.
        # Bail explicitly rather than crashing inside metric helpers.
        raise SystemExit("fetch_candles returned no data — HL API down?")
    actual_days = (
        candles["timestamp"].iloc[-1] - candles["timestamp"].iloc[0]
    ).days
    print(f"  → {len(candles)} bars, {actual_days} days "
          f"({candles['timestamp'].iloc[0].date()} → "
          f"{candles['timestamp'].iloc[-1].date()})")
    print()

    deploy_usd = 10_000.0

    print(f"{'Strategy':<28} {'Final':>10} {'Return':>10} "
          f"{'APR':>9} {'Sharpe':>7} {'MaxDD':>8} {'Trade':>5} {'TIM':>6}")
    print("-" * 95)

    print(fmt_row(hodl_metrics(candles, deploy_usd)))
    print(fmt_row(dca_metrics(candles, deploy_usd)))
    print()

    # Parameter sweep — answer: does any (lookback, trail) combo beat hold?
    sweeps = [
        (50, 0.15), (50, 0.25), (50, 0.35),
        (100, 0.15), (100, 0.25), (100, 0.35),
        (200, 0.15), (200, 0.25), (200, 0.35),
    ]
    for lb, tr in sweeps:
        print(fmt_row(await ath_metrics(candles, deploy_usd, lb, tr)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1825)
    args = parser.parse_args()
    asyncio.run(main(args.days))
