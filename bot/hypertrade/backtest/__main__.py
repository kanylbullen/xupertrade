"""CLI entrypoint for the backtest framework.

Usage:
    cd bot && uv run python -m hypertrade.backtest \\
        --strategy supertrend
    cd bot && uv run python -m hypertrade.backtest \\
        --strategy keltner_breakout --symbol ETH --timeframe 4h --days 365
    cd bot && uv run python -m hypertrade.backtest --all --days 180
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from hypertrade.backtest.runner import BacktestResult, run_backtest
from hypertrade.data.feed import fetch_candles
from hypertrade.strategies.registry import (
    list_strategies,
    load_all,
    get_strategy,
)

logger = logging.getLogger(__name__)


# HyperLiquid candleSnapshot caps each request — pull in chunks for long
# windows. The endpoint accepts up to ~5000 candles per call in practice.
async def _fetch_long_window(
    symbol: str, timeframe: str, days: int
) -> "object":  # pd.DataFrame, but avoid pandas import at module top for speed
    import pandas as pd

    bars_per_day = {
        "15m": 96, "15": 96,
        "1h": 24, "4h": 6, "1d": 1,
    }.get(timeframe, 24)

    target_bars = days * bars_per_day
    chunk_size = 4500
    if target_bars <= chunk_size:
        return await fetch_candles(symbol, timeframe, limit=target_bars)

    # Fetch in chunks of ~4500 bars by walking start_time backwards.
    # The data feed currently uses NOW − limit*tf as start; we need
    # multiple windows. Fall back: just fetch the maximum and accept that
    # very long backtests need API-level support that's not in scope.
    df = await fetch_candles(symbol, timeframe, limit=chunk_size)
    return df


def _format_apr_table(results: list[BacktestResult]) -> str:
    headers = ["strategy", "symbol", "tf", "trades", "win%", "APR%", "Sharpe", "MaxDD%"]
    widths = [22, 8, 6, 7, 6, 8, 7, 7]

    def row(values: list[str]) -> str:
        return " ".join(v.ljust(w) for v, w in zip(values, widths))

    lines = [row(headers), row(["-" * (w - 1) for w in widths])]
    for r in results:
        lines.append(row([
            r.strategy,
            r.symbol,
            r.timeframe,
            str(r.num_round_trips),
            f"{r.win_rate * 100:.0f}",
            f"{r.apr * 100:+.1f}",
            f"{r.sharpe:.2f}",
            f"{r.max_drawdown_pct * 100:.1f}",
        ]))
    return "\n".join(lines)


async def _run_one(
    strategy_name: str,
    symbol: str | None,
    timeframe: str | None,
    days: int,
    initial_equity: float,
    position_size: float,
    fee_rate: float,
    slippage_bps: float,
    save_to_db: bool = True,
) -> BacktestResult | None:
    try:
        strategy = get_strategy(strategy_name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None
    if symbol:
        strategy.symbol = symbol
    if timeframe:
        strategy.timeframe = timeframe

    print(f"Fetching {strategy.symbol} {strategy.timeframe} candles ({days}d)...", file=sys.stderr)
    candles = await _fetch_long_window(strategy.symbol, strategy.timeframe, days)
    if candles is None or candles.empty:
        print(f"No candles for {strategy.symbol} {strategy.timeframe}", file=sys.stderr)
        return None
    print(f"  → {len(candles)} bars", file=sys.stderr)

    result = await run_backtest(
        strategy, candles,
        initial_equity=initial_equity,
        position_size_usd=position_size,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )

    if save_to_db:
        try:
            from hypertrade.db.repo import Repository
            repo = Repository()
            try:
                rid = await repo.save_backtest_run(
                    strategy_name=result.strategy,
                    symbol=result.symbol,
                    timeframe=result.timeframe,
                    leverage=int(getattr(strategy, "leverage", 1) or 1),
                    period_start=result.start,
                    period_end=result.end,
                    days=result.days,
                    initial_equity=result.initial_equity,
                    final_equity=result.final_equity,
                    total_return_pct=result.total_return_pct,
                    apr=result.apr,
                    sharpe=result.sharpe,
                    max_drawdown_pct=result.max_drawdown_pct,
                    num_trades=result.num_trades,
                    num_round_trips=result.num_round_trips,
                    wins=result.wins,
                    losses=result.losses,
                    win_rate=result.win_rate,
                    fees_paid=result.fees_paid,
                    position_size_usd=position_size,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                )
                print(f"  → saved as backtest_run #{rid}", file=sys.stderr)
            finally:
                await repo.close()
        except Exception as e:
            print(f"  → DB save failed: {e}", file=sys.stderr)

    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy backtester")
    parser.add_argument("--strategy", help="Strategy name (omit with --all)")
    parser.add_argument("--all", action="store_true", help="Run every registered strategy")
    parser.add_argument("--symbol", default=None, help="Override the strategy's default symbol")
    parser.add_argument("--timeframe", default=None, help="Override the strategy's default timeframe")
    parser.add_argument("--days", type=int, default=180, help="Window length in days (default 180)")
    parser.add_argument("--initial-equity", type=float, default=10_000)
    parser.add_argument("--position-size", type=float, default=1_000)
    parser.add_argument("--fee-rate", type=float, default=0.00045)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--no-save", action="store_true",
                        help="Skip persisting result to backtest_runs table")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_all()

    if args.all:
        names = list_strategies()
    elif args.strategy:
        names = [args.strategy]
    else:
        parser.print_usage()
        return 2

    results: list[BacktestResult] = []
    for name in names:
        r = await _run_one(
            name, args.symbol, args.timeframe, args.days,
            args.initial_equity, args.position_size,
            args.fee_rate, args.slippage_bps,
            save_to_db=not args.no_save,
        )
        if r is None:
            continue
        results.append(r)
        if not args.all:
            print(r.format_summary())

    if args.all and results:
        print(_format_apr_table(results))

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
