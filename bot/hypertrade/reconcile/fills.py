"""Backfill missing `trades` rows from HyperLiquid's fill history.

Operator-triggered safety net for the failure mode where the bot
placed orders on HL but failed to write the matching `trades` row
(audit/incident PR #107: rotated DB password invalidated sibling
bots' DATABASE_URLs, so 6 fills between 05:00–10:36 UTC on 2026-05-13
never reached the DB). Querying HL directly is authoritative — fills
that exist on the exchange but not in our DB get inserted with
`strategy_name="reconciled"` since we have no signal context for
historical fills.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from hypertrade.db.models import Trade
from hypertrade.exchange.base import Exchange

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    examined: int = 0
    inserted: int = 0
    skipped: int = 0
    inserted_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "examined": self.examined,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "inserted_ids": list(self.inserted_ids),
        }


def _normalize_side(raw: str) -> str:
    s = (raw or "").strip().upper()
    if s in ("B", "BUY"):
        return "buy"
    if s in ("A", "S", "SELL"):
        return "sell"
    return raw or ""


async def reconcile_fills_from_hl(
    *,
    exchange: Exchange,
    repo,
    account_address: str | None = None,
    since_ms: int | None = None,
) -> ReconcileReport:
    """Pull HL fills and insert missing rows into `trades`.

    Dedup key is `trades.order_id == str(fill["oid"])`. New rows are
    tagged `strategy_name="reconciled"` (no signal attribution for
    historical fills). Tenant/mode/is_paper come from the Repository.
    """
    report = ReconcileReport()

    fills = await exchange.fetch_user_fills(
        address=account_address, since_ms=since_ms,
    )
    report.examined = len(fills)
    if not fills:
        return report

    oids = []
    for f in fills:
        oid = f.get("oid")
        if oid is None:
            continue
        oids.append(str(oid))
    if not oids:
        return report

    async with repo._session_factory() as session:
        existing_q = await session.execute(
            select(Trade.order_id).where(Trade.order_id.in_(oids))
        )
        existing: set[str] = {row for row in existing_q.scalars().all()}

        for f in fills:
            oid = f.get("oid")
            if oid is None:
                report.skipped += 1
                continue
            order_id = str(oid)
            if order_id in existing:
                report.skipped += 1
                continue
            try:
                size = float(f.get("sz", 0) or 0)
                price = float(f.get("px", 0) or 0)
                fee = float(f.get("fee", 0) or 0)
                pnl_raw = f.get("closedPnl")
                pnl = float(pnl_raw) if pnl_raw not in (None, "") else None
                ts_ms = int(f.get("time", 0) or 0)
            except (TypeError, ValueError):
                logger.warning("reconcile-fills: bad numeric in fill %s — skipping", f)
                report.skipped += 1
                continue
            executed_at = (
                datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                if ts_ms > 0
                else datetime.now(timezone.utc)
            )
            symbol = str(f.get("coin", "") or "")
            side = _normalize_side(str(f.get("side", "") or ""))
            trade = Trade(
                tenant_id=repo._tenant_id,
                order_id=order_id,
                strategy_name="reconciled",
                symbol=symbol,
                side=side,
                size=size,
                price=price,
                fee=fee,
                pnl=pnl,
                reason="backfilled from HL user_fills",
                is_paper=repo._is_paper,
                mode=repo._mode,
                timestamp=executed_at,
            )
            session.add(trade)
            try:
                await session.flush()
            except Exception:
                # Concurrent insert or unique-violation — count as skip.
                logger.warning(
                    "reconcile-fills: flush failed for oid=%s — skipping", order_id,
                )
                await session.rollback()
                continue
            report.inserted += 1
            existing.add(order_id)
            if trade.id is not None:
                report.inserted_ids.append(int(trade.id))
        await session.commit()

    logger.info(
        "reconcile-fills: examined=%d inserted=%d skipped=%d",
        report.examined, report.inserted, report.skipped,
    )
    return report
