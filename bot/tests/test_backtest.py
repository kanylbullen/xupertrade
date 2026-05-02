"""Backtest framework tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from hypertrade.backtest.metrics import (
    annualized_return,
    max_drawdown_pct,
    periods_per_year_for_timeframe,
    sharpe_ratio,
)
from hypertrade.backtest.runner import run_backtest
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy


# --- Metrics ---

def test_annualized_return_basic():
    # 50% gain over 365 days → 50% APR
    assert annualized_return(100, 150, 365) == pytest.approx(0.5, abs=1e-6)


def test_annualized_return_short_window_compounds():
    # 10% gain over 73 days → ~ (1.1)^5 - 1 = 61.05%
    assert annualized_return(100, 110, 73) == pytest.approx(
        (1.1) ** 5 - 1, abs=1e-6
    )


def test_annualized_return_zero_days():
    assert annualized_return(100, 110, 0) == 0.0


def test_annualized_return_total_loss():
    assert annualized_return(100, 0, 365) == -1.0


def test_max_drawdown_simple():
    # 100 → 120 → 60 → 80 → max DD = (120-60)/120 = 50%
    assert max_drawdown_pct([100, 120, 60, 80]) == pytest.approx(0.5)


def test_max_drawdown_monotone_up():
    assert max_drawdown_pct([100, 110, 120, 130]) == 0.0


def test_max_drawdown_empty():
    assert max_drawdown_pct([]) == 0.0


def test_sharpe_zero_when_too_few_samples():
    assert sharpe_ratio([0.01], 252) == 0.0
    assert sharpe_ratio([], 252) == 0.0


def test_sharpe_zero_when_no_variance():
    # Constant returns → stdev = 0 → Sharpe = 0
    assert sharpe_ratio([0.01, 0.01, 0.01, 0.01], 252) == 0.0


def test_sharpe_positive_for_positive_returns():
    # Mostly-positive low-vol returns → Sharpe should be positive
    returns = [0.001, 0.002, 0.001, 0.0005, 0.0015, 0.0008, 0.0012]
    s = sharpe_ratio(returns, periods_per_year=365)
    assert s > 0


def test_periods_per_year_timeframes():
    assert periods_per_year_for_timeframe("1d") == 365.0
    assert periods_per_year_for_timeframe("1h") == 365.0 * 24.0
    assert periods_per_year_for_timeframe("4h") == pytest.approx(365.0 * 24.0 / 4.0)
    assert periods_per_year_for_timeframe("15m") == pytest.approx(365.0 * 24.0 * 60.0 / 15.0)
    assert periods_per_year_for_timeframe("15") == pytest.approx(365.0 * 24.0 * 60.0 / 15.0)


# --- Runner ---


class _AlwaysFlipsStrategy(Strategy):
    """Toy strategy: open long on bar N, close on N+1, repeat. Used to
    drive a deterministic round trip in the backtest engine."""

    name = "test_always_flips"
    symbol = "TEST"
    timeframe = "1d"
    leverage = 1

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position = False

    async def on_candle(self, candles):
        if self._in_position:
            self._in_position = False
            return Signal(
                action=SignalAction.CLOSE_LONG, symbol=self.symbol,
                strategy_name=self.name, reason="close",
            )
        else:
            self._in_position = True
            return Signal(
                action=SignalAction.OPEN_LONG, symbol=self.symbol,
                strategy_name=self.name, reason="open",
            )


def _make_candles(prices: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV df with monotonic daily timestamps."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i, p in enumerate(prices):
        rows.append({
            "timestamp": base + timedelta(days=i),
            "open": p,
            "high": p * 1.001,
            "low": p * 0.999,
            "close": p,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


@pytest.mark.asyncio
async def test_runner_executes_long_round_trip():
    # Need >= 50 bars for default warmup. After warmup, prices rise 100→200
    # over 50 bars; toy strategy opens/closes alternately.
    prices = [100.0] * 50 + [100.0 + i * 2 for i in range(50)]
    df = _make_candles(prices)
    strat = _AlwaysFlipsStrategy()
    result = await run_backtest(
        strat, df,
        initial_equity=10_000,
        position_size_usd=1_000,
        fee_rate=0.0,  # zero fees → easier to verify PnL math
        slippage_bps=0.0,
        warmup_bars=50,
    )
    assert result.num_round_trips > 0
    # With rising prices and zero fees, total return must be positive
    assert result.total_return_pct > 0
    # Equity curve must have one entry per post-warmup bar
    assert len(result.equity_curve) == len(df) - 50


@pytest.mark.asyncio
async def test_runner_handles_empty_signals():
    """Strategy that emits no signals should leave equity exactly at start."""
    class _NoOp(Strategy):
        name = "noop"
        symbol = "TEST"
        timeframe = "1d"

        async def on_candle(self, _candles):
            return None

    df = _make_candles([100.0] * 100)
    result = await run_backtest(
        _NoOp(), df, initial_equity=10_000, fee_rate=0.0,
        slippage_bps=0.0, warmup_bars=50,
    )
    assert result.num_trades == 0
    assert result.final_equity == 10_000


@pytest.mark.asyncio
async def test_runner_raises_on_empty_df():
    with pytest.raises(ValueError):
        await run_backtest(_AlwaysFlipsStrategy(), pd.DataFrame())
