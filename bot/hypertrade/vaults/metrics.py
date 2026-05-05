"""Risk-adjusted metrics computed from a vault's (NAV, cumulative-PnL) series.

Pure functions — no I/O, no DB. Easy to unit-test on synthetic series.

**Why we compute returns from PnL deltas, not NAV deltas:**

A vault's NAV (`accountValueHistory`) reflects deposits and withdrawals
from every LP, not just trading P&L. If $1M is withdrawn, NAV drops
without anyone losing money. If $1M is deposited, NAV rises without
anyone earning anything. Sharpe / max-DD / ROI computed from raw NAV
deltas are flow-contaminated.

Cumulative PnL (`pnlHistory`) is pure strategy performance — it's the
sum of realized + unrealized P&L since vault inception, independent of
LP flows. Period return is `(pnl_cum_t - pnl_cum_{t-1}) / nav_{t-1}`,
which is the correct % return for the period regardless of what other
LPs did during it. Sharpe / DD / ROI are then computed from that
return series.

For the legacy case where pnl_cum is missing (e.g. our own NAV samples
collected before the pnl-aware schema), we fall back to NAV deltas with
a warning baked into the docstring.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from hypertrade.vaults.models import NavPoint, VaultMetrics


# Drop the leading "seed phase" of a vault's history: any sample where
# NAV is below this fraction of the eventual peak NAV is treated as
# "vault not yet capitalized". Without this trim, an early sample of
# nav=$100 followed by a +$1k of pnl produces a 1000% return and
# permanently dominates max-DD / Sharpe even years later. 1% is
# generous enough to keep most legitimate early growth while excluding
# the truly seed-stage noise.
SEED_PHASE_NAV_FRACTION = 0.01


def _trim_seed_phase(points: list[NavPoint]) -> list[NavPoint]:
    """Return `points` with the leading low-NAV seed phase dropped.

    A vault that grew from $0 → $10M would have early samples around
    $100. Period returns computed against $100 NAV produce 100x-1000x
    spikes that aren't real performance — they're the manager seeding
    the strategy. We treat any prefix of points whose NAV is < 1% of
    the series max as the seed phase and exclude it.
    """
    if not points:
        return points
    max_nav = max(p.nav for p in points)
    if max_nav <= 0:
        return points
    threshold = max_nav * SEED_PHASE_NAV_FRACTION
    # Drop the LEADING low-NAV samples only — once the vault has crossed
    # the threshold, even a temporary later dip back below it is real
    # performance (an actual drawdown), not seed noise.
    start = 0
    while start < len(points) and points[start].nav < threshold:
        start += 1
    return points[start:]


def _period_returns(points: list[NavPoint]) -> list[float]:
    """Compute per-period returns from cumulative-PnL deltas, normalized
    by NAV at the start of each period. Returns one value per consecutive
    pair of points. Drops any pair where the prior NAV was 0 (vault not
    seeded yet).

    Series-level pnl-vs-NAV fallback: if EVERY point in `points` has a
    non-None `pnl_cum`, use pnl deltas (flow-neutral). If ANY point is
    missing pnl_cum (legacy rows from before the pnl-aware schema, or
    HL points where pnlHistory didn't ship a matching timestamp), fall
    back to NAV deltas across the whole series — flow-contaminated but
    consistent. The two regimes are NEVER mixed in one series, because
    a boundary pair (None → 1.7M) would compute a giant artificial
    pnl_delta.

    The seed phase (leading samples with NAV < 1% of peak) is trimmed
    upstream by `_trim_seed_phase`.
    """
    points = _trim_seed_phase(points)
    has_pnl_everywhere = bool(points) and all(
        p.pnl_cum is not None for p in points
    )
    rets: list[float] = []
    for prev, cur in zip(points[:-1], points[1:]):
        if prev.nav <= 0:
            # Belt-and-braces: shouldn't trigger after _trim_seed_phase
            # but kept so the function is safe on any input.
            continue
        if has_pnl_everywhere:
            # mypy: pnl_cum can't be None inside this branch
            delta = cur.pnl_cum - prev.pnl_cum  # type: ignore[operator]
        else:
            delta = cur.nav - prev.nav
        rets.append(delta / prev.nav)
    return rets


def _bisect_at_or_before(
    points: list[NavPoint], target: datetime
) -> NavPoint | None:
    """Return the latest point with timestamp <= target, or None."""
    candidates = [p for p in points if p.timestamp <= target]
    return candidates[-1] if candidates else None


def _roi_over(points: list[NavPoint], days: int) -> float | None:
    """Cumulative return over the lookback window, computed by chaining
    period returns: (1+r1)*(1+r2)*...*(1+rn) - 1. Insensitive to LP
    flows because each `r_i` is pnl-based, not NAV-based."""
    if len(points) < 2:
        return None
    latest = points[-1]
    target = latest.timestamp - timedelta(days=days)
    if points[0].timestamp > target:
        # Need at least `days` of history to compute ROI(days)
        return None
    base = _bisect_at_or_before(points, target)
    if base is None:
        return None
    window = [p for p in points if p.timestamp >= base.timestamp]
    if len(window) < 2:
        return None
    rets = _period_returns(window)
    if not rets:
        return None
    cum = 1.0
    for r in rets:
        cum *= 1.0 + r
    return cum - 1.0


def max_drawdown(points: list[NavPoint]) -> float | None:
    """Worst peak-to-trough drawdown of the cumulative return curve.

    Builds the cumulative-return curve from period returns (flow-neutral)
    and finds the largest peak-to-trough drop. Returns the drop as a
    positive fraction (e.g. 0.32 = 32%). None when there isn't enough
    data."""
    if len(points) < 2:
        return None
    rets = _period_returns(points)
    if not rets:
        return None
    cum = 1.0
    peak = 1.0
    worst = 0.0
    for r in rets:
        cum *= 1.0 + r
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak
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
    """Annualized Sharpe ratio computed from per-period flow-neutral
    returns.

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

    rets = _period_returns(series)
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
    """One-shot: compute every metric we care about from a vault's
    (NAV, PnL) series."""
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
