"""Ghostfolio portfolio provider — open-source self-hosted multi-asset tracker.

Ghostfolio (https://ghostfol.io) is an AGPL-licensed portfolio tracker
that supports stocks, ETFs, funds, crypto, commodities, and currencies.
It runs as a self-hosted NestJS app backed by Postgres.

API auth is via Bearer token. Generate one in Ghostfolio settings →
Membership → "Security token" (newer versions call it API key). The
token is opaque per-user.

API basics (verified against ghostfolio v2.x):
  GET /api/v1/portfolio/holdings?dateRange=max
        — current holdings with marketValue, performance fields
  GET /api/v1/portfolio/details?dateRange=max
        — richer breakdown including allocations, accounts, summary

We use `/portfolio/holdings` for the simpler-to-render shape — one
position per asset with current value + performance vs. cost.

Live verification still pending — written from public Ghostfolio docs
and will be tightened on the user's first real call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from hypertrade.portfolio.models import CoinHolding, PortfolioSnapshot
from hypertrade.portfolio.providers.base import PortfolioProvider

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


def _safe_float(v, default: float | None = None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class GhostfolioProvider(PortfolioProvider):
    """Provider that talks to a self-hosted Ghostfolio instance."""

    name = "ghostfolio"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    async def fetch_snapshot(self) -> PortfolioSnapshot:
        url = f"{self.base_url}/api/v1/portfolio/holdings"
        params = {"dateRange": "max"}
        headers = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout_s),
        ) as session:
            try:
                async with session.get(
                    url, headers=headers, params=params,
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        logger.warning(
                            "ghostfolio /holdings HTTP %d: %s",
                            resp.status, body[:300],
                        )
                        return _empty(error=f"HTTP {resp.status}")
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning("ghostfolio fetch failed: %s", exc)
                return _empty(error=f"{type(exc).__name__}: {exc}"[:200])

        return _parse_holdings(data)


def _empty(error: str = "") -> PortfolioSnapshot:
    return PortfolioSnapshot(
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        ok=not error,
        error=error,
    )


def _parse_holdings(data: dict) -> PortfolioSnapshot:
    """Parse ghostfolio's `/portfolio/holdings` response.

    Documented shape (ghostfolio v2.x):
      {
        "holdings": [
          {
            "symbol": "ETH",
            "name": "Ethereum",
            "currency": "USD",
            "dataSource": "COINGECKO",
            "assetClass": "CRYPTOCURRENCY",
            "assetSubClass": "CRYPTOCURRENCY",
            "quantity": 44.5,
            "investment": 100000.0,        # cost basis in user's currency
            "marketPrice": 4343.56,
            "marketValue": 193288.42,
            "valueInBaseCurrency": 193288.42,
            "grossPerformance": 93288.42,  # absolute P&L
            "grossPerformancePercent": 0.93,  # decimal (0.93 = +93%)
            "netPerformance": 92500.0,
            ...
          }
        ]
      }
    """
    result = (data or {})
    holdings = result.get("holdings") or []
    if not isinstance(holdings, list):
        return _empty()

    coins: list[CoinHolding] = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        symbol = str(h.get("symbol") or "")
        if not symbol:
            continue
        quantity = _safe_float(h.get("quantity"), 0.0) or 0.0
        market_value = _safe_float(
            h.get("valueInBaseCurrency") or h.get("marketValue"), 0.0,
        ) or 0.0
        if quantity <= 0 and market_value <= 0:
            continue
        market_price = _safe_float(h.get("marketPrice"), 0.0) or 0.0
        if market_price == 0.0 and quantity > 0:
            market_price = market_value / quantity

        all_time_pnl = _safe_float(
            h.get("netPerformance") or h.get("grossPerformance"),
        )

        coins.append(CoinHolding(
            identifier=symbol,
            symbol=symbol,
            name=str(h.get("name") or symbol),
            icon="",
            rank=None,
            count=quantity,
            price_usd=market_price,
            value_usd=market_value,
            # Ghostfolio's holdings endpoint doesn't ship 24h change
            # directly; fold-in could come from the per-asset chart
            # endpoint later.
            price_change_24h_pct=None,
            price_change_7d_pct=None,
            pnl_24h_usd=None,
            pnl_all_time_usd=all_time_pnl,
            pnl_unrealized_usd=None,
            pnl_realized_usd=None,
            avg_buy_usd=None,
            avg_sell_usd=None,
            risk_score=None,
            liquidity_score=None,
            volatility_score=None,
        ))

    coins.sort(key=lambda c: c.value_usd, reverse=True)
    total_value = sum(c.value_usd for c in coins)
    total_all_time = sum((c.pnl_all_time_usd or 0.0) for c in coins)

    return PortfolioSnapshot(
        coins=coins,
        total_value_usd=total_value,
        total_pnl_24h_usd=0.0,
        total_pnl_all_time_usd=total_all_time,
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
    )
