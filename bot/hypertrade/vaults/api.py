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
    FollowerState,
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
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("vaultDetails(%s) failed: %s", address, exc)
        return None

    return await _parse_vault_details(data, address)


async def _parse_vault_details(
    data, fallback_address: str
) -> VaultDetails | None:
    """Shared parser for the `vaultDetails` JSON body. Returns None when
    the payload is missing or malformed."""
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
            # Build a {ts_ms: pnl_cum} lookup so we can pair each NAV
            # sample with its cumulative PnL when computing period
            # returns. HL aligns the two arrays by timestamp.
            pnl_by_ts: dict = {}
            for pp in pdata.get("pnlHistory", []) or []:
                if (isinstance(pp, (list, tuple)) and len(pp) >= 2):
                    try:
                        pnl_by_ts[int(pp[0])] = float(pp[1])
                    except (TypeError, ValueError):
                        continue
            for point in pdata.get("accountValueHistory", []) or []:
                if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                    continue
                ts_ms, nav_str = point[0], point[1]
                try:
                    ts_ms_int = int(ts_ms)
                    nav_history.append(
                        NavPoint(
                            timestamp=datetime.fromtimestamp(
                                ts_ms_int / 1000.0, tz=timezone.utc
                            ),
                            nav=float(nav_str),
                            pnl_cum=pnl_by_ts.get(ts_ms_int, 0.0),
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
            address=str(data.get("vaultAddress") or fallback_address),
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
        logger.warning(
            "vaultDetails(%s) parse failed: %s", fallback_address, exc
        )
        return None


async def fetch_user_vault_state(
    user_address: str,
    vault_address: str,
    session: aiohttp.ClientSession,
) -> tuple[VaultDetails | None, FollowerState | None]:
    """Fetch a vault's details *with* the user's follower state attached.

    HL accepts an optional `user` field on `vaultDetails`; when present
    it returns a `followerState` object with the user's current equity,
    unrealized P&L, all-time P&L, and entry time. This is the source of
    truth for "what is my position worth?" — far better than diffing
    `userVaultEquities` snapshots over time, which only sees principal
    movement and misses unrealized P&L entirely.

    Returns (details, follower_state). Either can be None on error /
    invalid address / user not a follower.
    """
    payload = {
        "type": "vaultDetails",
        "vaultAddress": vault_address,
        "user": user_address,
    }
    try:
        async with session.post(
            INFO_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=DETAIL_TIMEOUT_S),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(
            "vaultDetails(%s,user=%s) failed: %s",
            vault_address, user_address, exc,
        )
        return None, None

    if data is None or not isinstance(data, dict):
        return None, None

    # Re-use fetch_details parsing for the bulk of the payload.
    details = await _parse_vault_details(data, vault_address)

    fs = data.get("followerState")
    if not isinstance(fs, dict):
        return details, None

    try:
        entered_ms = int(fs.get("vaultEntryTime") or 0)
        locked_ms = int(fs.get("lockupUntil") or 0)
        follower = FollowerState(
            user_address=str(fs.get("user") or user_address).lower(),
            vault_address=vault_address.lower(),
            vault_equity_usd=float(fs.get("vaultEquity") or 0.0),
            unrealized_pnl_usd=float(fs.get("pnl") or 0.0),
            all_time_pnl_usd=float(fs.get("allTimePnl") or 0.0),
            days_following=int(fs.get("daysFollowing") or 0),
            entered_at=datetime.fromtimestamp(
                entered_ms / 1000.0, tz=timezone.utc
            ) if entered_ms > 0 else datetime.now(tz=timezone.utc),
            locked_until=(
                datetime.fromtimestamp(locked_ms / 1000.0, tz=timezone.utc)
                if locked_ms > 0 else None
            ),
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "follower state parse failed for %s/%s: %s",
            user_address, vault_address, exc,
        )
        follower = None

    return details, follower


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
