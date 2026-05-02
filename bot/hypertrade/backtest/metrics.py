"""Backtest performance metrics — pure functions, no I/O."""

from __future__ import annotations

import math


def annualized_return(
    initial: float, final: float, days: float
) -> float:
    """Return CAGR-style annualized return as a decimal (0.5 = 50%/yr).

    Returns 0.0 for non-positive duration or initial equity.
    """
    if initial <= 0 or days <= 0:
        return 0.0
    if final <= 0:
        return -1.0
    return (final / initial) ** (365.0 / days) - 1.0


def max_drawdown_pct(equity_curve: list[float]) -> float:
    """Maximum peak-to-trough drawdown over the curve, as a decimal.
    0.25 = 25% drawdown. Returns 0.0 for an empty / monotone-up curve."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def sharpe_ratio(
    period_returns: list[float],
    periods_per_year: float,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualized Sharpe ratio.

    `period_returns`  per-period decimal returns (e.g. daily 0.01 = +1%)
    `periods_per_year`  e.g. 252 for daily, 365*24 for hourly, etc.
    `risk_free_rate`  annual decimal; converted to per-period internally.

    Returns 0.0 when there aren't enough samples or stdev is zero.
    """
    n = len(period_returns)
    if n < 2:
        return 0.0
    mean = sum(period_returns) / n
    variance = sum((r - mean) ** 2 for r in period_returns) / (n - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return 0.0
    rf_per_period = risk_free_rate / periods_per_year if periods_per_year > 0 else 0.0
    excess_mean = mean - rf_per_period
    return (excess_mean / stdev) * math.sqrt(periods_per_year)


def periods_per_year_for_timeframe(timeframe: str) -> float:
    """Convert a timeframe string to periods-per-year for Sharpe scaling."""
    tf = timeframe.lower()
    if tf.endswith("d"):
        return 365.0 / max(1.0, float(tf[:-1] or 1))
    if tf.endswith("h"):
        return 365.0 * 24.0 / max(1.0, float(tf[:-1] or 1))
    if tf.endswith("m"):
        return 365.0 * 24.0 * 60.0 / max(1.0, float(tf[:-1] or 1))
    if tf.isdigit():
        # Bare number — assume minutes (matches HyperLiquid's "15" = 15min)
        return 365.0 * 24.0 * 60.0 / max(1.0, float(tf))
    return 365.0  # fallback: daily
