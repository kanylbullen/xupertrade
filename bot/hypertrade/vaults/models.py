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
    timestamp: datetime
    nav: float


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
