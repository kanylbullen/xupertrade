"""Daily vault scanner — fetches the catalog, computes metrics, persists snapshots,
and emits VaultQualified / VaultDisqualified events on state change.

Idempotent: safe to run more than once a day. Snapshots are uniqued on
(vault_address, snapshot_at) so a re-run with the same minute updates
the existing row instead of inserting a duplicate.
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
    fetch_details_batch,
)
from hypertrade.vaults.filters import FilterConfig, coarse_prefilter, evaluate
from hypertrade.vaults.metrics import compute_metrics
from hypertrade.vaults.models import VaultSnapshot

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.repo = repo
        self.event_bus = event_bus
        self.config = config or FilterConfig()
        self.detail_concurrency = detail_concurrency
        self.debounce_seconds = debounce_seconds
        # (address, new_state) -> last fired ts (epoch seconds)
        self._last_alert: dict[tuple[str, str], float] = {}

    async def poll(self) -> dict:
        """Run one full poll cycle. Returns a small summary dict for logs."""
        started = datetime.now(timezone.utc)
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
                return {
                    "candidates": 0,
                    "qualified": 0,
                    "scanned": len(catalog),
                }

            details = await fetch_details_batch(
                [c.address for c in candidates],
                concurrency=self.detail_concurrency,
                session=session,
            )

        qualified_count = 0
        new_qualified: list[tuple[str, str, VaultSnapshot]] = []
        new_disqualified: list[tuple[str, str, list[str]]] = []

        for summary in candidates:
            det = details.get(summary.address)
            if det is None:
                continue
            metrics = compute_metrics(det.nav_history)
            snap = VaultSnapshot(
                summary=summary, details=det, metrics=metrics, snapshot_at=started
            )
            verdict = evaluate(snap, self.config)
            if verdict.qualified:
                qualified_count += 1

            previously_qualified = await self._was_previously_qualified(summary.address)

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
            await self.repo.save_vault_snapshot(
                vault_address=summary.address,
                snapshot_at=started,
                aum_usd=summary.tvl_usd,
                nav=det.nav_history[-1].nav if det.nav_history else None,
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

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "vault poller: scanned=%d candidates=%d qualified=%d "
            "newly_qualified=%d newly_disqualified=%d elapsed=%.1fs",
            len(catalog),
            len(candidates),
            qualified_count,
            len(new_qualified),
            len(new_disqualified),
            elapsed,
        )
        return {
            "scanned": len(catalog),
            "candidates": len(candidates),
            "qualified": qualified_count,
            "newly_qualified": len(new_qualified),
            "newly_disqualified": len(new_disqualified),
            "elapsed_s": elapsed,
        }

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
