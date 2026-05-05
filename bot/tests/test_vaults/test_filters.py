"""Quality filter logic on synthetic snapshots."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from hypertrade.vaults.filters import (
    FilterConfig,
    coarse_prefilter,
    evaluate,
)
from hypertrade.vaults.models import (
    NavPoint,
    VaultDetails,
    VaultMetrics,
    VaultSnapshot,
    VaultSummary,
)


def _summary(**overrides) -> VaultSummary:
    base = dict(
        address="0x" + "ab" * 20,
        name="Test Vault",
        leader_address="0x" + "cd" * 20,
        tvl_usd=1_000_000.0,
        is_closed=False,
        relationship_type="normal",
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=400),
        apr=0.5,
    )
    base.update(overrides)
    return VaultSummary(**base)


def _details(**overrides) -> VaultDetails:
    base = dict(
        address="0x" + "ab" * 20,
        name="Test Vault",
        leader_address="0x" + "cd" * 20,
        description="",
        apr=0.5,
        leader_fraction=0.10,
        leader_commission=0.10,
        allow_deposits=True,
        is_closed=False,
        relationship_type="normal",
        follower_count=42,
        nav_history=[],
    )
    base.update(overrides)
    return VaultDetails(**base)


def _metrics(**overrides) -> VaultMetrics:
    base = dict(
        roi_7d=0.02,
        roi_30d=0.05,
        roi_90d=0.10,
        roi_180d=0.20,
        roi_365d=0.40,
        max_drawdown_pct=0.10,
        sharpe_180d=2.5,
    )
    base.update(overrides)
    return VaultMetrics(**base)


def _snap(summary=None, details=None, metrics=None) -> VaultSnapshot:
    return VaultSnapshot(
        summary=summary or _summary(),
        details=details or _details(),
        metrics=metrics or _metrics(),
        snapshot_at=datetime.now(tz=timezone.utc),
    )


def test_perfect_vault_qualifies():
    v = evaluate(_snap())
    assert v.qualified is True


def test_closed_vault_disqualified():
    v = evaluate(_snap(details=_details(is_closed=True)))
    assert v.qualified is False
    assert any(r.name == "open_to_deposits" and not r.passed for r in v.breakdown)


def test_too_young_disqualified():
    young = _summary(
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=50)
    )
    v = evaluate(_snap(summary=young))
    assert v.qualified is False
    assert any(r.name == "min_age" and not r.passed for r in v.breakdown)


def test_aum_too_small():
    v = evaluate(_snap(summary=_summary(tvl_usd=50_000)))
    assert v.qualified is False
    assert any(r.name == "aum_band" and not r.passed for r in v.breakdown)


def test_aum_too_big():
    v = evaluate(_snap(summary=_summary(tvl_usd=50_000_000)))
    assert v.qualified is False
    assert any(r.name == "aum_band" and not r.passed for r in v.breakdown)


def test_low_manager_equity():
    v = evaluate(_snap(details=_details(leader_fraction=0.01)))
    assert v.qualified is False
    assert any(r.name == "manager_equity" and not r.passed for r in v.breakdown)


def test_high_fee_disqualified():
    v = evaluate(_snap(details=_details(leader_commission=0.30)))
    assert v.qualified is False
    assert any(r.name == "profit_share_fee" and not r.passed for r in v.breakdown)


def test_negative_roi_90d_disqualified():
    v = evaluate(_snap(metrics=_metrics(roi_90d=-0.05)))
    assert v.qualified is False
    assert any(r.name == "roi_90d" and not r.passed for r in v.breakdown)


def test_drawdown_too_deep():
    v = evaluate(_snap(metrics=_metrics(max_drawdown_pct=0.50)))
    assert v.qualified is False
    assert any(r.name == "max_drawdown" and not r.passed for r in v.breakdown)


def test_low_sharpe_disqualified():
    v = evaluate(_snap(metrics=_metrics(sharpe_180d=0.5)))
    assert v.qualified is False
    assert any(r.name == "sharpe_180d" and not r.passed for r in v.breakdown)


def test_young_vault_waives_roi_365d():
    young = _summary(
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=200)
    )
    # young vault has no 365d ROI but passes via waiver
    v = evaluate(_snap(summary=young, metrics=_metrics(roi_365d=None)))
    assert v.qualified is True
    waived = next(r for r in v.breakdown if r.name == "roi_365d")
    assert waived.waived is True
    assert waived.passed is True


def test_coarse_prefilter_drops_obvious_misses():
    cands = [
        _summary(),  # qualifies coarse
        _summary(is_closed=True),  # closed
        _summary(relationship_type="child"),  # not normal
        _summary(tvl_usd=50_000),  # too small
        _summary(tvl_usd=50_000_000),  # too large
        _summary(
            created_at=datetime.now(tz=timezone.utc) - timedelta(days=30)
        ),  # too young
        _summary(apr=-0.5),  # negative apr
    ]
    out = coarse_prefilter(cands)
    assert len(out) == 1
    assert out[0].is_closed is False


def test_filter_config_overrides_apply():
    # Loosen Sharpe and confirm low-Sharpe vault now passes.
    snap = _snap(metrics=_metrics(sharpe_180d=0.8))
    v = evaluate(snap, FilterConfig(min_sharpe_180d=0.5))
    assert v.qualified is True
