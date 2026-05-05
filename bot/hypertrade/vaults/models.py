"""Plain dataclasses representing parsed HL vault API responses.

Kept distinct from `db/models.py` SQLAlchemy rows so the API client and
filter logic can be tested without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class VaultSummary:
    """One entry from the catalog endpoint."""

    address: str
    name: str
    leader_address: str
    tvl_usd: float
    is_closed: bool
    relationship_type: str
    created_at: datetime
    apr: float

    @property
    def age_days(self) -> int:
        delta = datetime.now(tz=self.created_at.tzinfo) - self.created_at
        return int(delta.total_seconds() // 86400)


@dataclass
class NavPoint:
    """One sample of vault state at a point in time. `nav` is total
    account value (deposits + withdrawals + cumulative pnl). `pnl_cum`
    is the cumulative net PnL since vault inception, or None when we
    don't have it (legacy rows from before the pnl-aware schema, or
    HL points where the pnlHistory timestamp didn't match). We store
    both so period returns can be computed flow-neutrally as
    `(pnl_cum_t - pnl_cum_{t-1}) / nav_{t-1}`. The metrics layer treats
    None as 'unknown' — it never silently substitutes 0 (which would
    create a giant artificial pnl_delta at the boundary)."""

    timestamp: datetime
    nav: float
    pnl_cum: float | None = None


@dataclass
class VaultDetails:
    """Per-vault deep payload from `vaultDetails`."""

    address: str
    name: str
    leader_address: str
    description: str
    apr: float
    leader_fraction: float       # manager equity share — "skin in game"
    leader_commission: float     # profit-share fee, decimal
    allow_deposits: bool
    is_closed: bool
    relationship_type: str
    follower_count: int
    nav_history: list[NavPoint] = field(default_factory=list)


@dataclass
class FollowerState:
    """Per-user view of a vault, from `vaultDetails(user=...)`."""

    user_address: str
    vault_address: str
    vault_equity_usd: float       # current value of user's stake
    unrealized_pnl_usd: float     # `pnl` in HL — currently-unrealized
    all_time_pnl_usd: float       # `allTimePnl` — lifetime P&L on this stake
    days_following: int
    # `vaultEntryTime` from HL — None when the field was missing/0 in the
    # response. Don't substitute "now" because that's user-misleading.
    entered_at: datetime | None
    locked_until: datetime | None


@dataclass
class VaultMetrics:
    """Computed risk-adjusted metrics from a NAV series."""

    roi_7d: float | None = None
    roi_30d: float | None = None
    roi_90d: float | None = None
    roi_180d: float | None = None
    roi_365d: float | None = None
    max_drawdown_pct: float | None = None
    sharpe_180d: float | None = None


@dataclass
class VaultSnapshot:
    """One full snapshot ready for persistence + filter evaluation."""

    summary: VaultSummary
    details: VaultDetails
    metrics: VaultMetrics
    snapshot_at: datetime
