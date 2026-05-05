"""CoinStats portfolio API client.

Wraps `GET /portfolio/coins`. Returns a `PortfolioSnapshot` ready for
dashboard rendering. Costs 8 credits per request, so callers should
cache aggressively (5-min TTL is the project default).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from hypertrade.portfolio.models import CoinHolding, PortfolioSnapshot

logger = logging.getLogger(__name__)

COINSTATS_URL = "https://openapiv1.coinstats.app/portfolio/coins"
DEFAULT_TIMEOUT_S = 30.0


def _safe_float(v, default: float | None = None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct(v, default: float | None = None) -> float | None:
    """Convert percentage-as-percent (e.g. 5.74 means +5.74%) to decimal."""
    f = _safe_float(v, None)
    if f is None:
        return default
    return f / 100.0


def _parse_holding(raw: dict) -> CoinHolding | None:
    """Defensively parse one entry from the `result` array."""
    if not isinstance(raw, dict):
        return None
    coin = raw.get("coin") or {}
    if not isinstance(coin, dict):
        return None

    identifier = str(coin.get("identifier") or "")
    symbol = str(coin.get("symbol") or "")
    if not identifier and not symbol:
        # Garbage entry — skip rather than show "?"
        return None

    count = _safe_float(raw.get("count"), 0.0) or 0.0
    price_block = raw.get("price") or {}
    price_usd = (
        _safe_float(price_block.get("USD"), 0.0)
        if isinstance(price_block, dict) else 0.0
    ) or 0.0
    value_usd = count * price_usd

    profit = raw.get("profit") or {}
    pnl_24h = (
        _safe_float((profit.get("hour24") or {}).get("USD"))
        if isinstance(profit, dict) else None
    )
    pnl_all = (
        _safe_float((profit.get("allTime") or {}).get("USD"))
        if isinstance(profit, dict) else None
    )
    pnl_unrealized = (
        _safe_float((profit.get("unrealized") or {}).get("USD"))
        if isinstance(profit, dict) else None
    )
    pnl_realized = (
        _safe_float((profit.get("realized") or {}).get("USD"))
        if isinstance(profit, dict) else None
    )

    avg_buy = (raw.get("averageBuy") or {})
    avg_sell = (raw.get("averageSell") or {})

    return CoinHolding(
        identifier=identifier,
        symbol=symbol,
        name=str(coin.get("name") or ""),
        icon=str(coin.get("icon") or ""),
        rank=int(coin["rank"]) if coin.get("rank") is not None else None,
        count=count,
        price_usd=price_usd,
        value_usd=value_usd,
        price_change_24h_pct=_pct(coin.get("priceChange24h")),
        price_change_7d_pct=_pct(coin.get("priceChange7d")),
        pnl_24h_usd=pnl_24h,
        pnl_all_time_usd=pnl_all,
        pnl_unrealized_usd=pnl_unrealized,
        pnl_realized_usd=pnl_realized,
        avg_buy_usd=(
            _safe_float(avg_buy.get("USD")) if isinstance(avg_buy, dict) else None
        ),
        avg_sell_usd=(
            _safe_float(avg_sell.get("USD")) if isinstance(avg_sell, dict) else None
        ),
        risk_score=_safe_float(raw.get("riskScore")),
        liquidity_score=_safe_float(raw.get("liquidityScore")),
        volatility_score=_safe_float(raw.get("volatilityScore")),
    )


async def fetch_portfolio_coins(
    api_key: str,
    share_token: str,
    *,
    passcode: str = "",
    include_risk_score: bool = True,
    session: aiohttp.ClientSession | None = None,
) -> PortfolioSnapshot:
    """Call CoinStats `/portfolio/coins` and return a parsed snapshot.

    Returns an EMPTY snapshot (not None) on auth/network failure so the
    caller can degrade gracefully — dashboard still renders, just empty.
    Logs the failure for diagnostic visibility.
    """
    if not api_key or not share_token:
        # Misconfiguration — caller usually checks this, but defend anyway.
        return PortfolioSnapshot(
            fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    headers = {
        "X-API-KEY": api_key,
        "sharetoken": share_token,
    }
    if passcode:
        headers["passcode"] = passcode

    params = {"includeRiskScore": "true" if include_risk_score else "false"}

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(
            COINSTATS_URL,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_S),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    "CoinStats /portfolio/coins HTTP %d: %s",
                    resp.status, body[:300],
                )
                return PortfolioSnapshot(
                    fetched_at=datetime.now(tz=timezone.utc).isoformat(),
                )
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("CoinStats /portfolio/coins failed: %s", exc)
        return PortfolioSnapshot(
            fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        )
    finally:
        if own_session:
            await session.close()

    coins: list[CoinHolding] = []
    for raw in (data or {}).get("result", []) or []:
        h = _parse_holding(raw)
        if h is not None:
            coins.append(h)

    coins.sort(key=lambda c: c.value_usd, reverse=True)

    total_value = sum(c.value_usd for c in coins)
    total_24h = sum((c.pnl_24h_usd or 0.0) for c in coins)
    total_all_time = sum((c.pnl_all_time_usd or 0.0) for c in coins)

    return PortfolioSnapshot(
        coins=coins,
        total_value_usd=total_value,
        total_pnl_24h_usd=total_24h,
        total_pnl_all_time_usd=total_all_time,
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        cached=False,
    )
