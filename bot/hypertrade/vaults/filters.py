"""Quality filters from the Phase 1 plan.

Pure logic: each function takes a `VaultSnapshot` (or its components)
and returns a deterministic verdict + a per-filter breakdown so the UI
can show *why* a vault failed.

Plan defaults (override via `FilterConfig`):
  age >= 180d, ROI 90d/180d/365d > 0%, max DD <= 25%,
  Sharpe(180d) > 1.5, manager equity >= 5%,
  AUM in [200k, 20M], profit-share fee <= 15%.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypertrade.vaults.models import VaultSnapshot


@dataclass
class FilterConfig:
    min_age_days: int = 180
    min_roi_90d: float = 0.0
    min_roi_180d: float = 0.0
    min_roi_365d: float = 0.0          # waived if vault < 365d
    max_drawdown_pct: float = 0.25
    min_sharpe_180d: float = 1.5
    min_manager_equity_pct: float = 0.05
    min_aum_usd: float = 200_000.0
    max_aum_usd: float = 20_000_000.0
    max_profit_share_pct: float = 0.15
    require_open_to_deposits: bool = True


@dataclass
class FilterResult:
    name: str
    passed: bool
    value: str       # human-readable observation
    threshold: str   # human-readable rule
    waived: bool = False  # true when the rule didn't apply (e.g. ROI 365d for young vault)


@dataclass
class QualifyVerdict:
    qualified: bool
    breakdown: list[FilterResult] = field(default_factory=list)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x*100:+.1f}%"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def evaluate(
    snap: VaultSnapshot, cfg: FilterConfig | None = None
) -> QualifyVerdict:
    cfg = cfg or FilterConfig()
    s, d, m = snap.summary, snap.details, snap.metrics
    out: list[FilterResult] = []

    # Open
    if cfg.require_open_to_deposits:
        passes = d.allow_deposits and not d.is_closed
        out.append(FilterResult(
            name="open_to_deposits",
            passed=passes,
            value=f"allowDeposits={d.allow_deposits}, isClosed={d.is_closed}",
            threshold="allowDeposits=True and isClosed=False",
        ))

    # Age
    age = s.age_days
    out.append(FilterResult(
        name="min_age",
        passed=age >= cfg.min_age_days,
        value=f"{age}d",
        threshold=f">= {cfg.min_age_days}d",
    ))

    # AUM band
    aum = s.tvl_usd
    out.append(FilterResult(
        name="aum_band",
        passed=cfg.min_aum_usd <= aum <= cfg.max_aum_usd,
        value=_usd(aum),
        threshold=f"{_usd(cfg.min_aum_usd)} – {_usd(cfg.max_aum_usd)}",
    ))

    # Manager equity
    out.append(FilterResult(
        name="manager_equity",
        passed=d.leader_fraction >= cfg.min_manager_equity_pct,
        value=_pct(d.leader_fraction),
        threshold=f">= {_pct(cfg.min_manager_equity_pct)}",
    ))

    # Profit-share fee
    out.append(FilterResult(
        name="profit_share_fee",
        passed=d.leader_commission <= cfg.max_profit_share_pct,
        value=_pct(d.leader_commission),
        threshold=f"<= {_pct(cfg.max_profit_share_pct)}",
    ))

    # ROI 90d / 180d / 365d
    out.append(FilterResult(
        name="roi_90d",
        passed=(m.roi_90d is not None and m.roi_90d > cfg.min_roi_90d),
        value=_pct(m.roi_90d),
        threshold=f"> {_pct(cfg.min_roi_90d)}",
    ))
    out.append(FilterResult(
        name="roi_180d",
        passed=(m.roi_180d is not None and m.roi_180d > cfg.min_roi_180d),
        value=_pct(m.roi_180d),
        threshold=f"> {_pct(cfg.min_roi_180d)}",
    ))

    young = age < 365
    if young:
        out.append(FilterResult(
            name="roi_365d",
            passed=True,
            value=_pct(m.roi_365d),
            threshold=f"> {_pct(cfg.min_roi_365d)} (waived: vault age {age}d < 365d)",
            waived=True,
        ))
    else:
        out.append(FilterResult(
            name="roi_365d",
            passed=(m.roi_365d is not None and m.roi_365d > cfg.min_roi_365d),
            value=_pct(m.roi_365d),
            threshold=f"> {_pct(cfg.min_roi_365d)}",
        ))

    # Max DD
    dd = m.max_drawdown_pct
    out.append(FilterResult(
        name="max_drawdown",
        passed=(dd is not None and dd <= cfg.max_drawdown_pct),
        value=_pct(dd),
        threshold=f"<= {_pct(cfg.max_drawdown_pct)}",
    ))

    # Sharpe
    sh = m.sharpe_180d
    out.append(FilterResult(
        name="sharpe_180d",
        passed=(sh is not None and sh > cfg.min_sharpe_180d),
        value=("n/a" if sh is None else f"{sh:.2f}"),
        threshold=f"> {cfg.min_sharpe_180d:.2f}",
    ))

    qualified = all(r.passed for r in out)
    return QualifyVerdict(qualified=qualified, breakdown=out)


def coarse_prefilter(
    summaries, cfg: FilterConfig | None = None
) -> list:
    """Return the subset of catalog summaries worth fetching details for.

    Cheap criteria only — avoid the expensive per-vault POST until a vault
    has a reasonable chance of passing the full filter. We're permissive
    here: a few false-positives are fine, but missing a real qualifier is
    not. The full filter applies after `compute_metrics`.
    """
    cfg = cfg or FilterConfig()
    out = []
    for s in summaries:
        if s.is_closed:
            continue
        if s.relationship_type != "normal":
            continue
        if not (cfg.min_aum_usd <= s.tvl_usd <= cfg.max_aum_usd):
            continue
        if s.age_days < cfg.min_age_days:
            continue
        # APR is from the catalog and reflects (typically) the trailing
        # ~30d window. A vault deeply negative on APR is unlikely to pass
        # the ROI 90d > 0 check, so we drop it cheaply.
        if s.apr <= 0:
            continue
        out.append(s)
    return out
