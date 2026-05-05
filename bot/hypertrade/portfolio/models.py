"""Plain dataclasses for parsed portfolio data.

Source-agnostic — the dashboard renders these regardless of which
provider (CoinStats today, others later) populated them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CoinHolding:
    """One coin position with current value + P&L. Mirrors CoinStats's
    `/portfolio/coins` shape, kept narrow so parallel providers can fit."""

    identifier: str            # provider-specific id (e.g. "ethereum")
    symbol: str                # e.g. "ETH"
    name: str                  # e.g. "Ethereum"
    icon: str                  # logo URL
    rank: int | None           # market-cap rank, may be missing for fakes
    count: float               # how many of the coin user holds
    price_usd: float           # current spot in USD
    value_usd: float           # = count * price_usd, precomputed for the UI

    # 24h price-change percent (decimal: 0.05 = +5%)
    price_change_24h_pct: float | None = None
    price_change_7d_pct: float | None = None

    # P&L on the user's stake
    pnl_24h_usd: float | None = None
    pnl_all_time_usd: float | None = None
    pnl_unrealized_usd: float | None = None
    pnl_realized_usd: float | None = None

    avg_buy_usd: float | None = None
    avg_sell_usd: float | None = None

    # Optional risk metrics (only when CoinStats was asked for them)
    risk_score: float | None = None
    liquidity_score: float | None = None
    volatility_score: float | None = None


@dataclass
class PortfolioSnapshot:
    """One point-in-time view of the user's portfolio.

    `ok` distinguishes a legitimately-empty portfolio (ok=True, coins=[])
    from a fetch failure (ok=False, coins=[]). Callers cache only
    successful responses — empty-but-ok still gets cached so a brand-new
    portfolio doesn't burn 8 credits per dashboard refresh, but real
    errors retry on the next request instead of pinning a 5-min outage.
    """

    coins: list[CoinHolding] = field(default_factory=list)
    total_value_usd: float = 0.0
    total_pnl_24h_usd: float = 0.0
    total_pnl_all_time_usd: float = 0.0
    fetched_at: str = ""        # ISO-8601 UTC
    cached: bool = False
    ok: bool = True
    error: str = ""
