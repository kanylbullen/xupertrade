"""Pure-function metric tests on synthetic NAV+PnL series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypertrade.vaults.metrics import (
    _annualization_factor,
    _period_returns,
    _roi_over,
    compute_metrics,
    max_drawdown,
    sharpe,
)
from hypertrade.vaults.models import NavPoint


def _series(
    nav_values: list[float],
    *,
    pnl_values: list[float] | None = None,
    step_days: float = 1.0,
    end: datetime | None = None,
) -> list[NavPoint]:
    """Build a NAV+PnL series ending at `end` with constant spacing.

    If `pnl_values` is None we synthesize cumulative PnL from NAV deltas
    so the legacy NAV-only tests still exercise the same maths."""
    end = end or datetime.now(tz=timezone.utc)
    if pnl_values is None:
        pnl_values = [0.0] + [
            sum(nav_values[i + 1] - nav_values[i] for i in range(j))
            for j in range(1, len(nav_values))
        ]
    return [
        NavPoint(
            timestamp=end - timedelta(days=step_days * (len(nav_values) - 1 - i)),
            nav=v,
            pnl_cum=pnl_values[i],
        )
        for i, v in enumerate(nav_values)
    ]


def test_max_drawdown_v_shape():
    pts = _series([100, 80, 60, 90, 110])
    # Drawdown computed off cumulative-return curve from period returns.
    # Period returns: -0.20, -0.25, +0.50, +0.222 (each pnl_delta == nav_delta
    # because we synth pnl_values as nav delta sums; nav stays the principal
    # divisor). Cumulative product peaks at start (1.0), troughs at .60,
    # ends at 1.10. Worst peak-to-trough = 0.40.
    assert abs(max_drawdown(pts) - 0.40) < 1e-9


def test_max_drawdown_monotone_up_is_zero():
    pts = _series([100, 110, 120, 130])
    assert max_drawdown(pts) == 0.0


def test_max_drawdown_too_few_points():
    assert max_drawdown([]) is None
    assert max_drawdown(_series([100])) is None


def test_max_drawdown_flow_neutral_pnl_based():
    """Same strategy P&L but two different NAV trajectories (one with a
    big withdrawal mid-period). Max-DD should be the same — it's pnl-based,
    not NAV-based.
    """
    end = datetime.now(tz=timezone.utc)
    # Vault makes +5% then loses 10% of that gain — pure strategy
    # Cumulative pnl: 0, +5, +4.5
    no_flow = [
        NavPoint(end - timedelta(days=2), nav=100, pnl_cum=0),
        NavPoint(end - timedelta(days=1), nav=105, pnl_cum=5.0),
        NavPoint(end,                     nav=104.5, pnl_cum=4.5),
    ]
    # Same strategy returns, but $50 withdrawn between t1 and t2.
    # NAV swings 100 → 105 → 54.5. Pnl deltas same as no-flow.
    with_flow = [
        NavPoint(end - timedelta(days=2), nav=100, pnl_cum=0),
        NavPoint(end - timedelta(days=1), nav=105, pnl_cum=5.0),
        NavPoint(end,                     nav=54.5, pnl_cum=4.5),
    ]
    a = max_drawdown(no_flow)
    b = max_drawdown(with_flow)
    assert a is not None and b is not None
    assert abs(a - b) < 1e-9


def test_roi_pnl_based_ignores_withdrawals():
    end = datetime.now(tz=timezone.utc)
    # Vault gained 10% over 90d. Without flows.
    no_flow = [
        NavPoint(end - timedelta(days=90), nav=100.0, pnl_cum=0.0),
        NavPoint(end - timedelta(days=45), nav=105.0, pnl_cum=5.0),
        NavPoint(end,                       nav=110.0, pnl_cum=10.0),
    ]
    # Same strategy, but $40 was withdrawn at the halfway point. NAV
    # series looks completely different; pnl is identical.
    with_flow = [
        NavPoint(end - timedelta(days=90), nav=100.0, pnl_cum=0.0),
        NavPoint(end - timedelta(days=45), nav=65.0,  pnl_cum=5.0),
        NavPoint(end,                       nav=68.0,  pnl_cum=10.0),
    ]
    a = _roi_over(no_flow, 90)
    b = _roi_over(with_flow, 90)
    assert a is not None and b is not None
    # Both should reflect ~+10% strategy ROI; the with-flow case should
    # NOT be -32% (which it would be on raw-NAV).
    assert abs(a - 0.10) < 0.02
    assert abs(b - a) < 0.05  # within 5pp of each other


def test_roi_over_returns_none_when_insufficient_history():
    pts = _series([100, 110], step_days=10)  # only 10 days
    assert _roi_over(pts, 90) is None


def test_sharpe_straight_line_is_none_zero_variance():
    # Constant NAV, constant pnl → all returns zero → std=0 → undefined.
    pts = [
        NavPoint(datetime.now(tz=timezone.utc) - timedelta(days=i), nav=100.0, pnl_cum=0.0)
        for i in range(10, -1, -1)
    ]
    assert sharpe(pts) is None


def test_sharpe_steady_uptrend_positive():
    end = datetime.now(tz=timezone.utc)
    # +1 pnl per day on $100 NAV → 1% daily return, very high Sharpe.
    pts = [
        NavPoint(end - timedelta(days=8 - i), nav=100.0 + i, pnl_cum=float(i))
        for i in range(9)
    ]
    s = sharpe(pts)
    assert s is not None
    assert s > 0


def test_sharpe_high_volatility_lower_than_smooth():
    end = datetime.now(tz=timezone.utc)
    # Smooth: +1 per day. Spiky: +2, -1, +2, -1, ... (same average)
    smooth = [
        NavPoint(end - timedelta(days=8 - i), nav=100 + i, pnl_cum=float(i))
        for i in range(9)
    ]
    spiky_pnls = [0, 2, 1, 3, 2, 4, 3, 5, 4]
    spiky = [
        NavPoint(end - timedelta(days=8 - i), nav=100.0, pnl_cum=float(p))
        for i, p in enumerate(spiky_pnls)
    ]
    s_smooth = sharpe(smooth)
    s_spiky = sharpe(spiky)
    assert s_smooth is not None and s_spiky is not None
    assert s_smooth > s_spiky


def test_annualization_factor_daily():
    pts = _series([100, 101, 102, 103], step_days=1)
    assert abs(_annualization_factor(pts) - 365.0) < 1e-6


def test_annualization_factor_weekly():
    pts = _series([100, 101, 102, 103], step_days=7)
    assert abs(_annualization_factor(pts) - 365 / 7) < 0.1


def test_compute_metrics_full_series():
    end = datetime.now(tz=timezone.utc)
    # 200d daily series with mild uptrend + a temporary dip.
    pts = []
    pnl = 0.0
    for d in range(200):
        if 50 <= d < 60:
            ret = -0.01  # 10d dip of -1% per day
        else:
            ret = 0.0005  # +0.05% per day baseline
        # NAV is principal-only here for simplicity; pnl tracks cumulative return on that NAV.
        nav = 100.0
        pnl += nav * ret
        pts.append(NavPoint(end - timedelta(days=200 - d), nav=nav, pnl_cum=pnl))
    m = compute_metrics(pts)
    assert m.roi_90d is not None
    assert m.roi_180d is not None
    assert m.roi_7d is not None
    assert m.max_drawdown_pct is not None and m.max_drawdown_pct > 0
    # 365d unavailable (only 200d of data)
    assert m.roi_365d is None


def test_period_returns_skips_seed_phase():
    """Points where prev.nav == 0 (vault not seeded yet) must be dropped
    so they don't blow up `pnl_delta / 0`."""
    end = datetime.now(tz=timezone.utc)
    pts = [
        NavPoint(end - timedelta(days=2), nav=0.0,   pnl_cum=0.0),  # seed
        NavPoint(end - timedelta(days=1), nav=100.0, pnl_cum=0.0),  # first real sample
        NavPoint(end,                     nav=110.0, pnl_cum=10.0),
    ]
    rets = _period_returns(pts)
    # Only the (100→110) pair survives; the (0→100) pair is skipped.
    assert len(rets) == 1
    assert abs(rets[0] - 0.10) < 1e-9


def test_period_returns_falls_back_when_pnl_missing_anywhere():
    """If ANY point in the series has pnl_cum=None (legacy row, or HL
    didn't ship the pnlHistory entry), the whole window falls back to
    NAV-delta returns. We never mix the two regimes within one series
    because a boundary pair (0 → real-pnl) would synthesize a giant
    artificial pnl_delta."""
    end = datetime.now(tz=timezone.utc)
    # Mixed series: one pre-pnl-aware row + new rows with real pnl_cum.
    pts = [
        NavPoint(end - timedelta(days=2), nav=100.0, pnl_cum=None),     # legacy
        NavPoint(end - timedelta(days=1), nav=105.0, pnl_cum=1_500_000.0),  # new
        NavPoint(end,                     nav=110.0, pnl_cum=1_500_500.0),
    ]
    rets = _period_returns(pts)
    # Should compute from NAV deltas: +5/100 = 0.05, +5/105 ≈ 0.0476.
    # NOT from the boundary pnl_delta (1.5M / 100 = 15000).
    assert len(rets) == 2
    assert abs(rets[0] - 0.05) < 1e-9
    assert max(rets) < 1.0  # sanity: no garbage 15000.0


def test_seed_phase_trim_drops_low_nav_leading_samples():
    """Real-world Growi-style scenario: vault grew from $0 → $7M peak.
    Early samples around $100 should NOT count toward max-DD because a
    +$1k pnl on $100 NAV computes as 1000% — that's seed dynamics, not
    strategy performance. Once NAV > 1% of peak, real returns matter."""
    end = datetime.now(tz=timezone.utc)
    # Day 1-3: seed phase ($100, $1k, $10k — all < 1% of $1M peak)
    # Day 4-10: real phase ($1M, $1.1M, $1.05M, ...)
    pts = [
        NavPoint(end - timedelta(days=10), nav=100.0,    pnl_cum=0.0),
        NavPoint(end - timedelta(days=9),  nav=1_000.0,  pnl_cum=900.0),
        NavPoint(end - timedelta(days=8),  nav=10_000.0, pnl_cum=9_000.0),
        NavPoint(end - timedelta(days=7),  nav=1_000_000.0, pnl_cum=991_000.0),
        NavPoint(end - timedelta(days=5),  nav=1_100_000.0, pnl_cum=1_091_000.0),
        NavPoint(end - timedelta(days=3),  nav=1_050_000.0, pnl_cum=1_041_000.0),
        NavPoint(end - timedelta(days=1),  nav=1_080_000.0, pnl_cum=1_071_000.0),
    ]
    dd = max_drawdown(pts)
    assert dd is not None
    # Real DD: peak $1.1M → trough $1.05M = ~4.5%. NOT 99% (which is
    # what we'd get if seed samples weren't trimmed).
    assert dd < 0.10  # well under 10%


def test_seed_phase_trim_preserves_real_drawdowns_late_in_history():
    """A vault that grows past the threshold and THEN dips back below
    it has a real drawdown — don't trim that. Trim only the LEADING
    seed-phase prefix."""
    end = datetime.now(tz=timezone.utc)
    pts = [
        # Seed (gets trimmed)
        NavPoint(end - timedelta(days=10), nav=100.0,    pnl_cum=0.0),
        NavPoint(end - timedelta(days=9),  nav=10_000.0, pnl_cum=9_900.0),
        # Real growth → peak
        NavPoint(end - timedelta(days=7),  nav=1_000_000.0, pnl_cum=999_900.0),
        NavPoint(end - timedelta(days=5),  nav=2_000_000.0, pnl_cum=1_999_900.0),
        # Catastrophic real drawdown — even though NAV ($5k) is below
        # 1% of peak (1% of $2M = $20k), this is a REAL loss, not seed
        # noise, so the cumulative-return curve must reflect it.
        NavPoint(end - timedelta(days=2),  nav=5_000.0, pnl_cum=4_900.0),
    ]
    dd = max_drawdown(pts)
    assert dd is not None
    # Drawdown should be enormous — peak $2M → trough $5k = ~99.75%.
    assert dd > 0.99


def test_period_returns_uses_pnl_when_available_everywhere():
    """When EVERY point has pnl_cum, returns are pnl-delta based
    (flow-neutral). NAV deltas are ignored."""
    end = datetime.now(tz=timezone.utc)
    # NAV looks volatile (deposits and withdrawals), but the strategy
    # actually made a clean +1% per period.
    pts = [
        NavPoint(end - timedelta(days=3), nav=1000.0, pnl_cum=0.0),
        NavPoint(end - timedelta(days=2), nav=2000.0, pnl_cum=10.0),  # +10 on 1000
        NavPoint(end - timedelta(days=1), nav=500.0,  pnl_cum=30.0),  # +20 on 2000
        NavPoint(end,                     nav=600.0,  pnl_cum=35.0),  # +5 on 500
    ]
    rets = _period_returns(pts)
    assert len(rets) == 3
    # +10/1000 = 0.010, +20/2000 = 0.010, +5/500 = 0.010 — all the same
    # despite wild NAV swings caused by simulated deposits/withdrawals.
    for r in rets:
        assert abs(r - 0.010) < 1e-9
