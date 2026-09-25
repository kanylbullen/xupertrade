"""CLI entrypoint for the backtest framework.

Usage:
    cd bot && uv run python -m hypertrade.backtest \\
        --strategy supertrend
    cd bot && uv run python -m hypertrade.backtest \\
        --strategy keltner_breakout --symbol ETH --timeframe 4h --days 365
    cd bot && uv run python -m hypertrade.backtest --all --days 180

Every run is saved to `backtest_runs` under a tenant — `--tenant-id`, else
`TENANT_ID` from the environment — unless `--no-save` is given. Exit
codes: 0 when every requested strategy ran (and was saved), 1 when a
strategy produced no result or a save failed, 2 on a usage error such as
saving with no tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from typing import TYPE_CHECKING

from hypertrade.backtest.runner import BacktestResult, run_backtest
from hypertrade.config import settings
from hypertrade.data.feed import fetch_candles
from hypertrade.strategies.registry import (
    list_strategies,
    load_all,
    get_strategy,
)

if TYPE_CHECKING:
    from hypertrade.db.repo import Repository

logger = logging.getLogger(__name__)


# HyperLiquid candleSnapshot caps each request — pull in chunks for long
# windows. The endpoint accepts up to ~5000 candles per call in practice.
async def _fetch_long_window(
    symbol: str, timeframe: str, days: int
) -> "object":  # pd.DataFrame, but avoid pandas import at module top for speed
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
    source: str = "hyperliquid",
) -> tuple[BacktestResult, int] | None:
    """Run one strategy. Returns the result and the strategy's leverage
    (saved with the run), or None when there is nothing to run — an
    unknown strategy or no candles; the reason is printed."""
    try:
        strategy = get_strategy(strategy_name)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None
    if symbol:
        strategy.symbol = symbol
    if timeframe:
        strategy.timeframe = timeframe

    print(
        f"Fetching {strategy.symbol} {strategy.timeframe} candles ({days}d) from {source}...",
        file=sys.stderr,
    )
    if source == "binance":
        from hypertrade.data.binance_dump import load_dump
        try:
            candles = await load_dump(strategy.symbol, strategy.timeframe, days=days)
        except Exception as e:
            print(f"Binance dump fetch failed: {e}", file=sys.stderr)
            return None
    else:
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
    return result, int(getattr(strategy, "leverage", 1) or 1)


async def _save(
    repo: Repository,
    result: BacktestResult,
    leverage: int,
    position_size: float,
    fee_rate: float,
    slippage_bps: float,
) -> int:
    return await repo.save_backtest_run(
        strategy_name=result.strategy,
        symbol=result.symbol,
        timeframe=result.timeframe,
        leverage=leverage,
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


def _tenant_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a UUID: {value!r}") from None


async def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--tenant-id", type=_tenant_uuid, default=None,
                        help="Tenant the saved runs belong to (default: TENANT_ID). "
                             "The dashboard's /backtests page shows only the "
                             "signed-in tenant's runs.")
    parser.add_argument("--source", default="hyperliquid",
                        choices=["hyperliquid", "binance"],
                        help="Data source: hyperliquid (default, ~1y) or binance (5+y dump)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.all:
        names = None
    elif args.strategy:
        names = [args.strategy]
    else:
        parser.print_usage()
        return 2

    # Settle where results go before spending minutes on backtests. A run
    # with no tenant used to be printed as "DB save failed" and exit 0 on
    # Postgres (tenant_id is NOT NULL there); a schema without that
    # constraint saved it where the dashboard never shows it.
    repo: Repository | None = None
    if not args.no_save:
        tenant_id = args.tenant_id or settings.tenant_id
        if not tenant_id:
            print(
                "error: saving to backtest_runs needs a tenant — pass "
                "--tenant-id <uuid>, set TENANT_ID, or use --no-save",
                file=sys.stderr,
            )
            return 2
        from hypertrade.db.repo import Repository
        try:
            repo = Repository(tenant_id=tenant_id)
        except ValueError:
            print(f"error: TENANT_ID is not a UUID: {tenant_id!r}", file=sys.stderr)
            return 2

    load_all()
    if names is None:
        names = list_strategies()

    results: list[BacktestResult] = []
    no_result: list[str] = []
    save_failed = False
    try:
        for name in names:
            run = await _run_one(
                name, args.symbol, args.timeframe, args.days,
                args.initial_equity, args.position_size,
                args.fee_rate, args.slippage_bps,
                source=args.source,
            )
            if run is None:
                no_result.append(name)
                continue
            result, leverage = run
            results.append(result)
            if not args.all:
                print(result.format_summary())
            if repo is None:
                continue
            try:
                rid = await _save(
                    repo, result, leverage,
                    args.position_size, args.fee_rate, args.slippage_bps,
                )
            except Exception as e:  # noqa: BLE001 — any failure must end non-zero
                # Stop here: whatever broke this save (DB down, schema,
                # tenant FK) breaks the next one too.
                print(f"  → DB save failed for {name}: {e}", file=sys.stderr)
                save_failed = True
                break
            print(f"  → saved as backtest_run #{rid}", file=sys.stderr)
    finally:
        if repo is not None:
            await repo.close()

    if args.all and results:
        print(_format_apr_table(results))

    if save_failed:
        print(
            "error: not every run was saved (use --no-save to run without "
            "the database)",
            file=sys.stderr,
        )
        return 1
    if no_result:
        print(f"error: no result for: {', '.join(no_result)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
