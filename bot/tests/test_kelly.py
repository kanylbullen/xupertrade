"""Verify Kelly fraction calculation in weekly_eval.StrategyStats."""

import pytest

from hypertrade.reports.weekly_eval import StrategyStats


def _stats_with_pnls(pnls: list[float]) -> StrategyStats:
    s = StrategyStats(name="test")
    for p in pnls:
        s.pnls.append(p)
        if p > 0:
            s.wins += 1
        elif p < 0:
            s.losses += 1
        s.realized_pnl += p
    return s


def test_kelly_none_when_too_few_trades():
    """Kelly requires >=10 decisive trades."""
    s = _stats_with_pnls([1, 1, -1, 1, -1])
    assert s.kelly_fraction is None


def test_kelly_none_when_no_losses():
    """b is undefined without losing trades."""
    s = _stats_with_pnls([1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
    assert s.kelly_fraction is None


def test_kelly_classic_60_pct_winrate_1_to_1():
    """60% wins at 1:1 RR → f* = (1*0.6 - 0.4)/1 = 0.2 (20%)."""
    pnls = [1.0] * 6 + [-1.0] * 4
    s = _stats_with_pnls(pnls)
    assert s.kelly_fraction is not None
    assert s.kelly_fraction == pytest.approx(0.2, abs=1e-9)
    assert s.half_kelly == pytest.approx(0.1, abs=1e-9)
    assert s.quarter_kelly == pytest.approx(0.05, abs=1e-9)


def test_kelly_negative_edge_clamped_to_zero():
    """Losing strategy → negative f*, clamped to 0."""
    pnls = [1.0] * 3 + [-1.0] * 7
    s = _stats_with_pnls(pnls)
    # b=1, p=0.3 → f* = (1*0.3 - 0.7)/1 = -0.4 → clamped to 0
    assert s.kelly_fraction == 0.0


def test_kelly_high_rr_low_winrate():
    """40% win rate at 3:1 RR → f* = (3*0.4 - 0.6)/3 = 0.2 (20%)."""
    pnls = [3.0] * 4 + [-1.0] * 6
    s = _stats_with_pnls(pnls)
    assert s.kelly_fraction == pytest.approx(0.2, abs=1e-9)


def test_avg_win_and_avg_loss():
    pnls = [2.0, 4.0, -1.0, -3.0]
    s = _stats_with_pnls(pnls)
    assert s.avg_win == pytest.approx(3.0)
    assert s.avg_loss == pytest.approx(2.0)
