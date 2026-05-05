"""Daily vault scanner — fetches the catalog, computes metrics, persists snapshots,
and emits VaultQualified / VaultDisqualified events on state change.

Idempotent: safe to run more than once a day. Snapshots are bucketed to
the UTC date (00:00:00) so rerunning the same day updates the existing
row instead of inserting a duplicate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

import aiohttp

from hypertrade.events.bus import EventBus
from hypertrade.events.types import VaultDisqualified, VaultQualified
from hypertrade.vaults.api import (
    DEFAULT_DETAIL_CONCURRENCY,
    fetch_catalog,
    fetch_details,
    fetch_details_batch,
    fetch_user_vault_equities,
)
from hypertrade.vaults.filters import FilterConfig, coarse_prefilter, evaluate
from hypertrade.vaults.metrics import compute_metrics
from hypertrade.vaults.models import NavPoint, VaultSnapshot

logger = logging.getLogger(__name__)


def _utc_day_bucket(now: datetime) -> datetime:
    """Truncate to UTC midnight so re-runs in the same day update one row."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class VaultPoller:
    """Holds config and a 24h debounce dict to avoid re-firing alerts on
    each tick. The debounce key is (address, new_state)."""

    def __init__(
        self,
        repo,
        event_bus: EventBus | None = None,
        config: FilterConfig | None = None,
        detail_concurrency: int = DEFAULT_DETAIL_CONCURRENCY,
        debounce_seconds: float = 86400.0,
        track_user_address: str = "",
    ) -> None:
        self.repo = repo
        self.event_bus = event_bus
        self.config = config or FilterConfig()
        self.detail_concurrency = detail_concurrency
        self.debounce_seconds = debounce_seconds
        # Public mainnet wallet whose vault deposits we'll track. Empty
        # string disables user-position tracking entirely.
        self.track_user_address = track_user_address.strip().lower()
        # (address, new_state) -> last fired ts (epoch seconds)
        self._last_alert: dict[tuple[str, str], float] = {}

    async def poll(self) -> dict:
        """Run one full poll cycle. Returns a small summary dict for logs."""
        started = datetime.now(timezone.utc)
        snapshot_at = _utc_day_bucket(started)
        async with aiohttp.ClientSession() as session:
            try:
                catalog = await fetch_catalog(session=session)
            except Exception:
                logger.exception("vault poller: catalog fetch failed")
                return {"error": "catalog_fetch_failed"}

            candidates = coarse_prefilter(catalog, self.config)
            logger.info(
                "vault poller: %d/%d candidates after coarse prefilter",
                len(candidates),
                len(catalog),
            )

            if not candidates:
                details = {}
            else:
                details = await fetch_details_batch(
                    [c.address for c in candidates],
                    concurrency=self.detail_concurrency,
                    session=session,
                )

        qualified_count = 0
        new_qualified: list[tuple[str, str, VaultSnapshot]] = []
        new_disqualified: list[tuple[str, str, list[str]]] = []
        candidate_addresses: set[str] = set()

        for summary in candidates:
            candidate_addresses.add(summary.address)
            det = details.get(summary.address)
            if det is None:
                continue

            # Persist NAV history first so compute_metrics sees the merged
            # series — own-collected daily snapshots augment HL's sparse
            # ~5-10d allTime view over time.
            await self.repo.upsert_vault(
                address=summary.address,
                name=summary.name,
                leader_address=summary.leader_address,
                description=det.description,
                created_at=summary.created_at,
                profit_share_pct=det.leader_commission,
                relationship_type=summary.relationship_type,
            )
            if det.nav_history:
                await self.repo.append_nav_history(
                    summary.address,
                    [(p.timestamp, p.nav) for p in det.nav_history],
                )

            merged_history = await self._load_merged_history(summary.address, det.nav_history)
            metrics = compute_metrics(merged_history)
            # Replace the API-only history with the merged series so the rest
            # of the loop (including event payload) reflects what we scored on.
            det.nav_history = merged_history
            snap = VaultSnapshot(
                summary=summary,
                details=det,
                metrics=metrics,
                snapshot_at=snapshot_at,
            )
            verdict = evaluate(snap, self.config)
            if verdict.qualified:
                qualified_count += 1

            previously_qualified = await self._was_previously_qualified(summary.address)

            await self.repo.save_vault_snapshot(
                vault_address=summary.address,
                snapshot_at=snapshot_at,
                aum_usd=summary.tvl_usd,
                nav=merged_history[-1].nav if merged_history else None,
                leader_equity_pct=det.leader_fraction,
                depositor_count=det.follower_count,
                apr=det.apr,
                age_days=summary.age_days,
                roi_7d=metrics.roi_7d,
                roi_30d=metrics.roi_30d,
                roi_90d=metrics.roi_90d,
                roi_180d=metrics.roi_180d,
                roi_365d=metrics.roi_365d,
                max_drawdown_pct=metrics.max_drawdown_pct,
                sharpe_180d=metrics.sharpe_180d,
                qualified=verdict.qualified,
                filter_breakdown_json=json.dumps(
                    [asdict(r) for r in verdict.breakdown]
                ),
                allow_deposits=det.allow_deposits,
                is_closed=det.is_closed,
            )

            if verdict.qualified and not previously_qualified:
                new_qualified.append((summary.address, summary.name, snap))
            elif previously_qualified and not verdict.qualified:
                failed = [r.name for r in verdict.breakdown if not r.passed]
                new_disqualified.append((summary.address, summary.name, failed))

        # Catch vaults that USED to qualify but no longer survive the coarse
        # pre-filter (closed, AUM out of band, gone from catalog, ...).
        # Without this step they'd silently linger in /vaults until aged out.
        dropout_disq = await self._emit_dropouts_for(
            candidate_addresses, snapshot_at=snapshot_at
        )
        new_disqualified.extend(dropout_disq)

        if self.event_bus is not None:
            for address, name, snap in new_qualified:
                if not self._should_fire(address, "qualified"):
                    continue
                await self.event_bus.publish(
                    VaultQualified(
                        address=address,
                        name=name,
                        apr=snap.details.apr,
                        aum_usd=snap.summary.tvl_usd,
                        sharpe_180d=snap.metrics.sharpe_180d or 0.0,
                        leader_equity_pct=snap.details.leader_fraction,
                    )
                )
            for address, name, failed in new_disqualified:
                if not self._should_fire(address, "disqualified"):
                    continue
                await self.event_bus.publish(
                    VaultDisqualified(
                        address=address,
                        name=name,
                        failed_filters=",".join(failed),
                    )
                )

        # Track the user's own vault deposits if a tracking address is set.
        # Done after the main scan so any user-held vault that's already in
        # `vaults` has fresh metadata; vaults the user holds that AREN'T
        # in our candidates get on-demand vaultDetails fetched.
        user_positions = 0
        if self.track_user_address:
            try:
                user_positions = await self._poll_user_positions()
            except Exception:
                logger.exception("vault poller: user-position poll failed")

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "vault poller: scanned=%d candidates=%d qualified=%d "
            "newly_qualified=%d newly_disqualified=%d "
            "user_positions=%d elapsed=%.1fs",
            len(catalog),
            len(candidates),
            qualified_count,
            len(new_qualified),
            len(new_disqualified),
            user_positions,
            elapsed,
        )
        return {
            "scanned": len(catalog),
            "candidates": len(candidates),
            "qualified": qualified_count,
            "newly_qualified": len(new_qualified),
            "newly_disqualified": len(new_disqualified),
            "user_positions": user_positions,
            "elapsed_s": elapsed,
        }

    async def _poll_user_positions(self) -> int:
        """Refresh `user_vault_entries` from HL's userVaultEquities.

        For any vault the user holds that's NOT already in our `vaults`
        table, fetch its details on-demand so the dashboard has metadata
        + scoring even for vaults that don't pass the coarse pre-filter
        (small AUM, young, fee too high — user can hold them anyway).
        """
        equities = await fetch_user_vault_equities(self.track_user_address)
        if not equities:
            return 0

        async with aiohttp.ClientSession() as session:
            for entry in equities:
                addr = str(entry.get("vaultAddress") or "").lower()
                if not addr:
                    continue
                try:
                    equity = float(entry.get("equity") or 0.0)
                except (TypeError, ValueError):
                    equity = 0.0
                locked_ms = entry.get("lockedUntilTimestamp")
                locked_until = None
                if locked_ms:
                    try:
                        locked_until = datetime.fromtimestamp(
                            float(locked_ms) / 1000.0, tz=timezone.utc
                        )
                    except (TypeError, ValueError):
                        locked_until = None

                # Make sure we have metadata for this vault — user can hold
                # vaults that fail our coarse filter, and we still want to
                # show name/leader/age on the dashboard.
                vault = await self.repo.get_vault(addr)
                if vault is None:
                    det = await fetch_details(addr, session)
                    if det is not None:
                        await self.repo.upsert_vault(
                            address=det.address,
                            name=det.name,
                            leader_address=det.leader_address,
                            description=det.description,
                            # We don't have catalogue createTimeMillis
                            # without a catalog scan, so leave it None
                            # rather than fabricate. The next daily
                            # catalog scan will fill it in if visible.
                            created_at=None,
                            profit_share_pct=det.leader_commission,
                            relationship_type=det.relationship_type,
                        )
                        if det.nav_history:
                            await self.repo.append_nav_history(
                                addr,
                                [(p.timestamp, p.nav) for p in det.nav_history],
                            )

                try:
                    await self.repo.upsert_user_vault_entry(
                        user_address=self.track_user_address,
                        vault_address=addr,
                        equity_usd=equity,
                        locked_until=locked_until,
                    )
                except Exception:
                    logger.exception(
                        "vault poller: upsert_user_vault_entry(%s) failed",
                        addr,
                    )
        return len(equities)

    async def _load_merged_history(
        self, address: str, fresh: list[NavPoint]
    ) -> list[NavPoint]:
        """Combine HL's allTime samples with what we've appended to
        `vault_nav_history` over time. Dedup by timestamp; sort ascending."""
        try:
            stored = await self.repo.vault_nav_for(address)
        except Exception:
            logger.exception("vault poller: vault_nav_for(%s) failed", address)
            return fresh
        merged: dict[datetime, float] = {p.timestamp: p.nav for p in stored}
        for p in fresh:
            merged[p.timestamp] = p.nav
        return [NavPoint(timestamp=ts, nav=nav)
                for ts, nav in sorted(merged.items())]

    async def _emit_dropouts_for(
        self,
        candidate_addresses: set[str] | list[str],
        *,
        snapshot_at: datetime | None = None,
    ) -> list[tuple[str, str, list[str]]]:
        """Find vaults that were qualified in their latest snapshot but did
        not appear in the current candidate set (e.g. closed, AUM moved
        out of band, dropped from catalog). Persist a disqualified snapshot
        and return them so callers can publish events.
        """
        cand_set = set(candidate_addresses)
        try:
            previously = await self.repo.latest_qualified_vaults()
        except Exception:
            logger.exception("vault poller: latest_qualified_vaults failed")
            return []

        out: list[tuple[str, str, list[str]]] = []
        snapshot_at = snapshot_at or _utc_day_bucket(datetime.now(timezone.utc))
        for vault, snap in previously:
            if vault.address in cand_set:
                continue
            failed = ["coarse_prefilter_dropout"]
            try:
                await self.repo.save_vault_snapshot(
                    vault_address=vault.address,
                    snapshot_at=snapshot_at,
                    aum_usd=snap.aum_usd,
                    nav=snap.nav,
                    leader_equity_pct=snap.leader_equity_pct,
                    depositor_count=snap.depositor_count,
                    apr=snap.apr,
                    age_days=snap.age_days,
                    roi_7d=snap.roi_7d,
                    roi_30d=snap.roi_30d,
                    roi_90d=snap.roi_90d,
                    roi_180d=snap.roi_180d,
                    roi_365d=snap.roi_365d,
                    max_drawdown_pct=snap.max_drawdown_pct,
                    sharpe_180d=snap.sharpe_180d,
                    qualified=False,
                    filter_breakdown_json=json.dumps(
                        [{"name": "coarse_prefilter_dropout",
                          "passed": False,
                          "value": "no longer in coarse candidate set",
                          "threshold": "must appear in catalogue + pass cheap rules",
                          "weight": 1.0}]
                    ),
                    allow_deposits=snap.allow_deposits,
                    is_closed=snap.is_closed,
                )
            except Exception:
                logger.exception(
                    "vault poller: failed to write dropout snapshot for %s",
                    vault.address,
                )
                continue
            out.append((vault.address, vault.name or "", failed))
        return out

    async def _was_previously_qualified(self, address: str) -> bool:
        prev = await self.repo.latest_vault_snapshot(address)
        return bool(prev and prev.qualified)

    def _should_fire(self, address: str, new_state: str) -> bool:
        key = (address, new_state)
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_alert.get(key, 0.0)
        if now - last < self.debounce_seconds:
            return False
        self._last_alert[key] = now
        return True
