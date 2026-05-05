"""Risk-adjusted metrics computed from a NAV (account value) series.

Pure functions over `list[NavPoint]` — no I/O, no DB. Easy to unit-test
on synthetic series. NAV resolution from HL is variable (~5-10 days for
allTime view), so we treat each sample as one observation and infer the
period from the timestamp gaps.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from hypertrade.vaults.models import NavPoint, VaultMetrics


def _bisect_at_or_before(
    points: list[NavPoint], target: datetime
) -> NavPoint | None:
    """Return the latest point with timestamp <= target, or None."""
    candidates = [p for p in points if p.timestamp <= target]
    return candidates[-1] if candidates else None


def _roi_over(points: list[NavPoint], days: int) -> float | None:
    """Return ROI over the given lookback in *days*. None if insufficient
    history. Computed against the latest sample."""
    if len(points) < 2:
        return None
    latest = points[-1]
    target = latest.timestamp - timedelta(days=days)
    earliest = points[0]
    # Need at least `days` of history before "now".
    if earliest.timestamp > target:
        return None
    base = _bisect_at_or_before(points, target)
    if base is None or base.nav <= 0:
        return None
    return (latest.nav - base.nav) / base.nav


def max_drawdown(points: list[NavPoint]) -> float | None:
    """Return the worst peak-to-trough drawdown as a positive fraction.
    e.g. 0.32 means the vault dropped 32% from a prior peak. None when
    there isn't enough data to form a peak-trough pair."""
    if len(points) < 2:
        return None
    peak = points[0].nav
    worst = 0.0
    for p in points:
        if p.nav > peak:
            peak = p.nav
        if peak > 0:
            dd = (peak - p.nav) / peak
            if dd > worst:
                worst = dd
    return worst


def _annualization_factor(points: list[NavPoint]) -> float:
    """Periods per year inferred from the median gap between samples.
    Defaults to 365 when only one gap is available (assume daily)."""
    if len(points) < 3:
        return 365.0
    gaps = []
    for prev, cur in zip(points[:-1], points[1:]):
        delta = (cur.timestamp - prev.timestamp).total_seconds()
        if delta > 0:
            gaps.append(delta)
    if not gaps:
        return 365.0
    gaps.sort()
    median_s = gaps[len(gaps) // 2]
    median_d = max(median_s / 86400.0, 1e-3)
    return 365.0 / median_d


def sharpe(
    points: list[NavPoint],
    *,
    window_days: int | None = None,
    risk_free_rate: float = 0.0,
) -> float | None:
    """Annualized Sharpe ratio computed from per-period returns.

    `window_days` slices to the last N days of samples (None = all).
    Risk-free is annual; subtracted from the annualized mean return.
    Returns None when fewer than 3 returns are available or std dev is 0.
    """
    if not points:
        return None
    series = points
    if window_days is not None:
        cutoff = points[-1].timestamp - timedelta(days=window_days)
        series = [p for p in points if p.timestamp >= cutoff]
    if len(series) < 4:
        return None

    rets: list[float] = []
    for prev, cur in zip(series[:-1], series[1:]):
        if prev.nav <= 0:
            continue
        rets.append((cur.nav - prev.nav) / prev.nav)
    if len(rets) < 3:
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return None

    periods_per_year = _annualization_factor(series)
    ann_mean = mean * periods_per_year
    ann_std = std * math.sqrt(periods_per_year)
    return (ann_mean - risk_free_rate) / ann_std


def compute_metrics(points: list[NavPoint]) -> VaultMetrics:
    """One-shot: compute every metric we care about from a NAV series."""
    return VaultMetrics(
        roi_7d=_roi_over(points, 7),
        roi_30d=_roi_over(points, 30),
        roi_90d=_roi_over(points, 90),
        roi_180d=_roi_over(points, 180),
        roi_365d=_roi_over(points, 365),
        max_drawdown_pct=max_drawdown(points),
        sharpe_180d=sharpe(points, window_days=180),
    )


# Convenience used by tests
def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
