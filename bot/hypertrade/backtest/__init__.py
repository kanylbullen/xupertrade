"""Strategy backtest framework.

Lets you replay historical OHLCV against any registered strategy without
touching a live exchange. Returns Sharpe, APR, max drawdown, win rate,
and a trade list.

Usage:
    cd bot && uv run python -m hypertrade.backtest \\
        --strategy supertrend --days 365

The backtest pulls candles from HyperLiquid's REST candle API (same
endpoint the live data feed uses) and feeds them to the strategy in
chronological order. Fills are simulated at the candle close + a
configurable slippage; fees are deducted at the standard taker rate.
"""

from hypertrade.backtest.runner import BacktestResult, run_backtest
from hypertrade.backtest.metrics import (
    sharpe_ratio,
    max_drawdown_pct,
    annualized_return,
)

__all__ = [
    "BacktestResult",
    "run_backtest",
    "sharpe_ratio",
    "max_drawdown_pct",
    "annualized_return",
]
