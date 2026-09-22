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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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
            # Wrap each insert in a SAVEPOINT so a unique-violation
            # (concurrent insert / dedup race) rolls back ONLY that
            # row instead of nuking every previously-flushed insert
            # in this run. session.commit() at the end then persists
            # only the savepoints that succeeded.
            try:
                async with session.begin_nested():
                    session.add(trade)
                    await session.flush()
            except IntegrityError:
                logger.warning(
                    "reconcile-fills: integrity error for oid=%s — skipping",
                    order_id,
                )
                report.skipped += 1
                continue
            except SQLAlchemyError:
                logger.exception(
                    "reconcile-fills: db error for oid=%s — skipping", order_id,
                )
                report.skipped += 1
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


# ----------------------------------------------------------------------
# Pricing an orphan close from the exchange's own fill history.
#
# Reconcile used to close a DB row it could not explain with
# `exit_price = entry_price` and `pnl = 0.0` and no `Trade` row at all,
# so the money that actually moved never reached per-strategy PnL,
# /eval, /kelly, the dashboard or the daily-loss counter
# (`bot/reports/analysis-2026-09-15.md` § 2). The exchange knows what
# the close filled at — ask it.
# ----------------------------------------------------------------------


@dataclass
class ClosingFillSummary:
    """The fills that closed one DB row, collapsed to one price."""

    price: float
    """Size-weighted average fill price."""

    size: float
    """Total fill size matched (may be less than the row's size)."""

    fee: float
    """Sum of the matched fills' fees, pro-rated to `size`."""

    order_id: str | None
    """`oid` (or `tid`) of the first matched fill, for `Trade.order_id`."""

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "size": self.size,
            "fee": self.fee,
            "order_id": self.order_id,
        }


def _fill_order_id(fill: dict) -> str | None:
    for key in ("oid", "tid"):
        value = fill.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def select_closing_fills(
    fills: list[dict],
    *,
    symbol: str,
    position_side: str,
    since_ms: int | None = None,
) -> list[dict]:
    """Fills on `symbol` that REDUCE a `position_side` position.

    A long is closed by a sell, a short by a buy. Ordered oldest-first
    so the caller consumes them in the order they happened. `since_ms`
    (the row's `opened_at`) keeps a previous position's closing fills on
    the same coin out of the match.
    """
    want = "sell" if position_side == "long" else "buy"
    matched: list[dict] = []
    for f in fills:
        if str(f.get("coin", "") or "") != symbol:
            continue
        if _normalize_side(str(f.get("side", "") or "")) != want:
            continue
        try:
            ts = int(f.get("time", 0) or 0)
        except (TypeError, ValueError):
            continue
        if since_ms is not None and ts < since_ms:
            continue
        matched.append(f)
    matched.sort(key=lambda f: int(f.get("time", 0) or 0))
    return matched


def summarize_closing_fills(
    fills: list[dict], *, size: float,
) -> ClosingFillSummary | None:
    """Collapse `fills` into one price + fee for a row of `size`.

    Consumes fills oldest-first until `size` is covered; the last fill
    is partially consumed if it overshoots, and its fee is pro-rated by
    the consumed fraction. Returns None when nothing usable is found —
    the caller then falls back to the mid price, and if that fails too,
    leaves the row open rather than inventing a close.
    """
    if size <= 0:
        return None
    remaining = float(size)
    notional = 0.0
    consumed = 0.0
    fee_total = 0.0
    order_id: str | None = None
    for f in fills:
        try:
            fill_size = float(f.get("sz", 0) or 0)
            fill_price = float(f.get("px", 0) or 0)
            fill_fee = float(f.get("fee", 0) or 0)
        except (TypeError, ValueError):
            logger.warning("reconcile-price: bad numeric in fill %s — skipping", f)
            continue
        if fill_size <= 0 or fill_price <= 0:
            continue
        take = min(fill_size, remaining)
        share = take / fill_size
        notional += fill_price * take
        fee_total += fill_fee * share
        consumed += take
        remaining -= take
        if order_id is None:
            order_id = _fill_order_id(f)
        if remaining <= 1e-12:
            break
    if consumed <= 0:
        return None
    return ClosingFillSummary(
        price=notional / consumed,
        size=consumed,
        fee=fee_total,
        order_id=order_id,
    )


def realized_pnl(
    *, side: str, entry_price: float, exit_price: float,
    size: float, fee: float = 0.0,
) -> float:
    """Side-aware realised PnL for one closed row, net of `fee`.

    `side` is the POSITION side (long/short), not the closing order's.
    """
    gross = (
        (exit_price - entry_price) * size
        if side == "long"
        else (entry_price - exit_price) * size
    )
    return gross - fee
