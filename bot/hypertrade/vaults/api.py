"""HyperLiquid vault API client.

Two endpoints:
  GET https://stats-data.hyperliquid.xyz/Mainnet/vaults
      → catalogue (~14 MB, 9k+ entries)
  POST https://api.hyperliquid.xyz/info {"type":"vaultDetails","vaultAddress":"0x..."}
      → per-vault deep payload incl. NAV history

Shapes are documented in `docs/hyperliquid-vaults-api.md`. This module
only parses what we need; raw fields are dropped.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from hypertrade.vaults.models import (
    NavPoint,
    VaultDetails,
    VaultSummary,
)

logger = logging.getLogger(__name__)

CATALOG_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
INFO_URL = "https://api.hyperliquid.xyz/info"

# Bounded concurrency for per-vault fetches: HL is generous but we don't
# want to hammer them. 5 in flight is plenty given <50 candidates.
DEFAULT_DETAIL_CONCURRENCY = 5

# Catalog can be large; allow more time than the default.
CATALOG_TIMEOUT_S = 60.0
DETAIL_TIMEOUT_S = 30.0


async def fetch_catalog(
    session: aiohttp.ClientSession | None = None,
) -> list[VaultSummary]:
    """Download the full vault catalogue and return parsed summaries."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(
            CATALOG_URL,
            timeout=aiohttp.ClientTimeout(total=CATALOG_TIMEOUT_S),
        ) as resp:
            resp.raise_for_status()
            raw = await resp.json(content_type=None)
    finally:
        if own_session:
            await session.close()

    out: list[VaultSummary] = []
    for entry in raw:
        try:
            s = entry["summary"]
            out.append(
                VaultSummary(
                    address=s["vaultAddress"],
                    name=s.get("name", ""),
                    leader_address=s.get("leader", ""),
                    tvl_usd=float(s.get("tvl", 0.0)),
                    is_closed=bool(s.get("isClosed", False)),
                    relationship_type=(
                        s.get("relationship", {}).get("type", "normal")
                    ),
                    created_at=datetime.fromtimestamp(
                        s["createTimeMillis"] / 1000.0, tz=timezone.utc
                    ),
                    apr=float(entry.get("apr", 0.0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("vault catalog: skipping malformed entry: %s", exc)
            continue
    logger.info("vault catalog: fetched %d entries", len(out))
    return out


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


async def fetch_details(
    address: str, session: aiohttp.ClientSession
) -> VaultDetails | None:
    """POST vaultDetails for one vault. Returns None on miss/error.

    The `vaultDetails` payload is undocumented and we've seen `null` returned
    for invalid addresses. All field reads are guarded so a single bad
    vault can't abort the whole `fetch_details_batch` gather.
    """
    payload = {"type": "vaultDetails", "vaultAddress": address}
    try:
        async with session.post(
            INFO_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=DETAIL_TIMEOUT_S),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("vaultDetails(%s) failed: %s", address, exc)
        return None

    if data is None or not isinstance(data, dict):
        return None

    try:
        nav_history: list[NavPoint] = []
        portfolio = data.get("portfolio") or []
        if not isinstance(portfolio, list):
            portfolio = []
        for entry in portfolio:
            if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                continue
            period, pdata = entry[0], entry[1]
            # `allTime` gives the longest series; that's what we want for
            # Sharpe and max-DD over the lifetime.
            if period != "allTime" or not isinstance(pdata, dict):
                continue
            for point in pdata.get("accountValueHistory", []) or []:
                if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                    continue
                ts_ms, nav_str = point[0], point[1]
                try:
                    nav_history.append(
                        NavPoint(
                            timestamp=datetime.fromtimestamp(
                                float(ts_ms) / 1000.0, tz=timezone.utc
                            ),
                            nav=float(nav_str),
                        )
                    )
                except (TypeError, ValueError):
                    continue
            break

        followers = data.get("followers") or []
        relationship = data.get("relationship") or {}
        # `followers` is capped at 100 in the response, so this is
        # "≥ 100" for popular vaults. Stored as-is; the dashboard could
        # render "100+" when the cap is reached.
        follower_count = (
            len(followers) if isinstance(followers, list) else 0
        )

        return VaultDetails(
            address=str(data.get("vaultAddress") or address),
            name=str(data.get("name") or ""),
            leader_address=str(data.get("leader") or ""),
            description=str(data.get("description") or ""),
            apr=_safe_float(data.get("apr")),
            leader_fraction=_safe_float(data.get("leaderFraction")),
            leader_commission=_safe_float(data.get("leaderCommission")),
            allow_deposits=bool(data.get("allowDeposits", False)),
            is_closed=bool(data.get("isClosed", False)),
            relationship_type=(
                str(relationship.get("type", "normal"))
                if isinstance(relationship, dict)
                else "normal"
            ),
            follower_count=follower_count,
            nav_history=sorted(nav_history, key=lambda p: p.timestamp),
        )
    except Exception as exc:
        logger.warning("vaultDetails(%s) parse failed: %s", address, exc)
        return None


async def fetch_user_vault_equities(
    user_address: str, session: aiohttp.ClientSession | None = None
) -> list[dict]:
    """Return the user's stakes in each HL vault.

    Response shape: `[{vaultAddress, equity (str USD), lockedUntilTimestamp (ms)}]`
    Empty list if the user holds no vaults. Returns [] on any error so the
    poller can degrade gracefully.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    payload = {"type": "userVaultEquities", "user": user_address}
    try:
        async with session.post(
            INFO_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=DETAIL_TIMEOUT_S),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        # ValueError covers JSONDecodeError when HL returns an HTML error
        # page or otherwise malformed body — keep the documented "[] on
        # any error" contract.
        logger.warning("userVaultEquities(%s) failed: %s", user_address, exc)
        return []
    finally:
        if own_session:
            await session.close()

    if not isinstance(data, list):
        return []
    return data


async def fetch_details_batch(
    addresses: list[str],
    *,
    concurrency: int = DEFAULT_DETAIL_CONCURRENCY,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, VaultDetails]:
    """Fetch many vault details with bounded concurrency."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    sem = asyncio.Semaphore(concurrency)

    async def one(addr: str) -> tuple[str, VaultDetails | None]:
        async with sem:
            try:
                return addr, await fetch_details(addr, session)
            except Exception:  # belt-and-braces: never abort the whole batch
                logger.exception("fetch_details(%s) raised — skipping", addr)
                return addr, None

    try:
        results = await asyncio.gather(
            *(one(a) for a in addresses), return_exceptions=False
        )
    finally:
        if own_session:
            await session.close()

    return {addr: det for addr, det in results if det is not None}
