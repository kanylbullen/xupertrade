"""Pure-function metric tests on synthetic NAV series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypertrade.vaults.metrics import (
    _annualization_factor,
    _roi_over,
    compute_metrics,
    max_drawdown,
    sharpe,
)
from hypertrade.vaults.models import NavPoint


def _series(
    nav_values: list[float], *, step_days: float = 1.0, end: datetime | None = None
) -> list[NavPoint]:
    """Build a NAV series ending at `end` with constant spacing."""
    end = end or datetime.now(tz=timezone.utc)
    return [
        NavPoint(
            timestamp=end - timedelta(days=step_days * (len(nav_values) - 1 - i)),
            nav=v,
        )
        for i, v in enumerate(nav_values)
    ]


def test_max_drawdown_v_shape():
    pts = _series([100, 80, 60, 90, 110])
    # Worst point reached 60 from peak 100 → 40% DD.
    assert max_drawdown(pts) == 0.4


def test_max_drawdown_monotone_up_is_zero():
    pts = _series([100, 110, 120, 130])
    assert max_drawdown(pts) == 0.0


def test_max_drawdown_double_dip():
    pts = _series([100, 90, 110, 80, 120])
    # Highest peak before each trough: 100→90 = 10%, 110→80 = ~27.3%.
    assert abs(max_drawdown(pts) - (110 - 80) / 110) < 1e-9


def test_max_drawdown_too_few_points():
    assert max_drawdown([]) is None
    assert max_drawdown(_series([100])) is None


def test_roi_over_basic():
    pts = _series([100, 110, 121], step_days=45)  # 90 days total
    assert abs(_roi_over(pts, 90) - 0.21) < 1e-6


def test_roi_over_returns_none_when_insufficient_history():
    pts = _series([100, 110], step_days=10)  # only 10 days
    assert _roi_over(pts, 90) is None


def test_sharpe_straight_line_is_none_zero_variance():
    # Constant NAV → all returns zero → std=0 → undefined.
    pts = _series([100] * 10)
    assert sharpe(pts) is None


def test_sharpe_steady_uptrend_positive():
    pts = _series([100, 101, 102, 103, 104, 105, 106, 107])
    s = sharpe(pts)
    assert s is not None
    assert s > 0


def test_sharpe_high_volatility_lower_than_smooth():
    smooth = _series([100, 101, 102, 103, 104, 105, 106, 107])
    spiky = _series([100, 90, 110, 95, 115, 100, 120, 105])
    s_smooth = sharpe(smooth)
    s_spiky = sharpe(spiky)
    assert s_smooth is not None and s_spiky is not None
    # Smooth uptrend has higher risk-adjusted return.
    assert s_smooth > s_spiky


def test_annualization_factor_daily():
    pts = _series([100, 101, 102, 103], step_days=1)
    assert abs(_annualization_factor(pts) - 365.0) < 1e-6


def test_annualization_factor_weekly():
    pts = _series([100, 101, 102, 103], step_days=7)
    # 365/7 ≈ 52.14
    assert abs(_annualization_factor(pts) - 365 / 7) < 0.1


def test_compute_metrics_full_series():
    # Build a 200d daily series with mild uptrend + a temporary dip.
    end = datetime.now(tz=timezone.utc)
    navs = []
    for d in range(200):
        if 50 <= d < 60:
            navs.append(100 - (d - 50))  # 10d dip down to 90
        else:
            navs.append(100 + d * 0.05)  # +5%/100d drift
    pts = [
        NavPoint(timestamp=end - timedelta(days=200 - i), nav=v)
        for i, v in enumerate(navs)
    ]
    m = compute_metrics(pts)
    assert m.roi_90d is not None
    assert m.roi_180d is not None
    assert m.roi_7d is not None
    assert m.max_drawdown_pct is not None and m.max_drawdown_pct > 0
    # 365d unavailable (only 200d of data)
    assert m.roi_365d is None
