"""Rotki portfolio provider — open-source self-hosted alternative to CoinStats.

Rotki (https://rotki.com) is an AGPL-licensed crypto portfolio tracker.
It runs as a server you host yourself; we hit its REST API on the
`base_url` you point us at.

API basics (verified against rotki >=1.34):
  POST /api/1/users/<username>           — log in (returns session cookie)
  GET  /api/1/balances?async_query=false — current aggregated balances
                                            across all linked accounts
  GET  /api/1/users                      — list users (also tells us if
                                            the server is unlocked)

Rotki's auth is session-based, not API-key. We log in on first call and
re-use the aiohttp cookie jar; on 401 we re-login and retry once.

A real Rotki query can be slow (seconds — it pulls from many sources)
which is why the API endpoint layer caches the snapshot in Redis. Don't
spam the rotki backend; let the cache do its job.

Async query mode: Rotki supports `async_query=true` which returns a
task ID and lets you poll. We use the synchronous mode here because
the dashboard render is itself a single round trip and 5-min caching
amortizes the cost — async would complicate the call site without
saving end-to-end latency for the typical small portfolio.

Live verification still pending — written from public Rotki docs and
will be tightened on the user's first real call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from hypertrade.portfolio.models import CoinHolding, PortfolioSnapshot
from hypertrade.portfolio.providers.base import PortfolioProvider

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0  # rotki balance queries can take a few seconds


def _safe_float(v, default: float | None = None) -> float | None:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class RotkiProvider(PortfolioProvider):
    """Provider that talks to a self-hosted Rotki instance over HTTP."""

    name = "rotki"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_s = timeout_s
        # Cookie jar persisted across calls so we keep the rotki session
        # warm. Re-instantiated on logout / 401.
        self._jar: aiohttp.CookieJar | None = None

    async def fetch_snapshot(self) -> PortfolioSnapshot:
        """One-shot: ensure session, fetch balances, parse to snapshot.

        Returns an empty snapshot (logged) on any failure so the
        dashboard renders cleanly.
        """
        async with aiohttp.ClientSession(
            cookie_jar=self._get_jar(),
            timeout=aiohttp.ClientTimeout(total=self.timeout_s),
        ) as session:
            try:
                data = await self._fetch_balances(session)
            except _RotkiUnauthorized:
                # Session expired — log in fresh and try once more.
                self._jar = None
                async with aiohttp.ClientSession(
                    cookie_jar=self._get_jar(),
                    timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                ) as fresh:
                    try:
                        await self._login(fresh)
                        data = await self._fetch_balances(fresh)
                    except Exception as exc:
                        logger.warning("rotki: re-auth + fetch failed: %s", exc)
                        return _empty_snapshot()
            except Exception as exc:
                logger.warning("rotki: fetch failed: %s", exc)
                return _empty_snapshot()

        return _parse_balances(data)

    def _get_jar(self) -> aiohttp.CookieJar:
        if self._jar is None:
            self._jar = aiohttp.CookieJar(unsafe=True)
        return self._jar

    async def _fetch_balances(self, session: aiohttp.ClientSession) -> dict:
        """Single attempt to GET /api/1/balances. Raises _RotkiUnauthorized
        when the session isn't valid so the caller can re-auth."""
        url = f"{self.base_url}/api/1/balances"
        params = {"async_query": "false", "ignore_cache": "false"}
        async with session.get(url, params=params) as resp:
            if resp.status in (401, 409):
                # 401 = no session, 409 = no user logged in (rotki convention)
                raise _RotkiUnauthorized()
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(
                    f"rotki balances HTTP {resp.status}: {body[:300]}"
                )
            return await resp.json(content_type=None)

    async def _login(self, session: aiohttp.ClientSession) -> None:
        """POST /api/1/users/<username> to start a session. Idempotent
        on rotki's side."""
        url = f"{self.base_url}/api/1/users/{self.username}"
        payload = {
            "password": self.password,
            "sync_approval": "unknown",
            "resume_from_backup": False,
        }
        async with session.post(url, json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(
                    f"rotki login HTTP {resp.status}: {body[:300]}"
                )


class _RotkiUnauthorized(Exception):
    """Internal sentinel — session expired or never started."""


def _empty_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
    )


def _parse_balances(data: dict) -> PortfolioSnapshot:
    """Parse rotki's `/api/1/balances` response.

    Documented shape (rotki 1.34+):
      {
        "result": {
          "assets": {
            "ETH":  {"amount": "44.4987", "usd_value": "193281.34"},
            "BTC":  {"amount": "0.5",     "usd_value": "55000.00"},
            ...
          },
          "liabilities": {...},
          "location": {...}    # per-exchange breakdown, ignored here
        }
      }

    We unfold `assets` into `CoinHolding` rows. Rotki doesn't ship
    per-coin price-change or P&L in this endpoint — that lives in
    separate /trades and /history endpoints. v1 displays just count +
    USD value; richer fields wired later when the user has more data.
    """
    result = (data or {}).get("result") or {}
    if not isinstance(result, dict):
        return _empty_snapshot()

    assets = result.get("assets") or {}
    if not isinstance(assets, dict):
        return _empty_snapshot()

    coins: list[CoinHolding] = []
    for symbol, info in assets.items():
        if not isinstance(info, dict):
            continue
        amount = _safe_float(info.get("amount"), 0.0) or 0.0
        usd = _safe_float(info.get("usd_value"), 0.0) or 0.0
        if amount <= 0 and usd <= 0:
            continue
        price = (usd / amount) if amount > 0 else 0.0
        coins.append(CoinHolding(
            identifier=symbol,    # rotki uses symbol-as-identifier
            symbol=symbol,
            name=symbol,           # no full name from this endpoint
            icon="",
            rank=None,
            count=amount,
            price_usd=price,
            value_usd=usd,
            # Rotki balances endpoint doesn't ship these — leave None
            price_change_24h_pct=None,
            price_change_7d_pct=None,
            pnl_24h_usd=None,
            pnl_all_time_usd=None,
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
    return PortfolioSnapshot(
        coins=coins,
        total_value_usd=total_value,
        total_pnl_24h_usd=0.0,        # not provided by /balances
        total_pnl_all_time_usd=0.0,
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
        cached=False,
    )
