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


def closing_side(position_side: str) -> str:
    """The order side that REDUCES a `position_side` position."""
    return "sell" if position_side == "long" else "buy"


def select_closing_fills(
    fills: list[dict],
    *,
    symbol: str,
    position_side: str,
    since_ms: int | None = None,
    exclude_order_ids: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Fills on `symbol` that REDUCE a `position_side` position.

    A long is closed by a sell, a short by a buy. Ordered oldest-first
    so the caller consumes them in the order they happened. `since_ms`
    (the row's `opened_at`) keeps a previous position's closing fills on
    the same coin out of the match.

    `exclude_order_ids` drops fills that are ALREADY booked as `trades`
    rows. Without it, another strategy's signal-path close on the same
    coin (daily_long_0830's 08:15 sell while hash_supertrend holds a BTC
    long, say) would be handed to the orphan as an authoritative "(fill)"
    price and PnL — money attributed twice, to two strategies.
    """
    want = closing_side(position_side)
    excluded = frozenset(exclude_order_ids or ())
    matched: list[dict] = []
    for f in fills:
        if str(f.get("coin", "") or "") != symbol:
            continue
        if _normalize_side(str(f.get("side", "") or "")) != want:
            continue
        oid = _fill_order_id(f)
        if oid is not None and oid in excluded:
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


def fill_order_ids(fills: list[dict]) -> set[str]:
    """Every `oid`/`tid` present in `fills`, as strings."""
    out: set[str] = set()
    for f in fills:
        oid = _fill_order_id(f)
        if oid is not None:
            out.add(oid)
    return out


@dataclass
class _LedgerEntry:
    """One fill, with how much of it is still unspent."""

    coin: str
    side: str  # normalized buy/sell
    time_ms: int
    price: float
    fee: float  # fee of the WHOLE fill; pro-rated on partial take
    size: float
    remaining: float
    order_id: str | None


_COVER_EPS = 1e-12


def _parse_entries(fills: list[dict]) -> list[_LedgerEntry]:
    entries: list[_LedgerEntry] = []
    for f in fills:
        try:
            size = float(f.get("sz", 0) or 0)
            price = float(f.get("px", 0) or 0)
            fee = float(f.get("fee", 0) or 0)
            ts = int(f.get("time", 0) or 0)
        except (TypeError, ValueError):
            logger.warning("reconcile-price: bad numeric in fill %s — skipping", f)
            continue
        if size <= 0 or price <= 0:
            continue
        entries.append(_LedgerEntry(
            coin=str(f.get("coin", "") or ""),
            side=_normalize_side(str(f.get("side", "") or "")),
            time_ms=ts,
            price=price,
            fee=fee,
            size=size,
            remaining=size,
            order_id=_fill_order_id(f),
        ))
    entries.sort(key=lambda e: e.time_ms)
    return entries


def _consume(
    entries: list[_LedgerEntry], size: float, *, commit: bool,
) -> ClosingFillSummary | None:
    """Take exactly `size` from `entries`, oldest first.

    FULL coverage is required. A partial match is not a close price: an
    orphan row is one the exchange no longer holds, so its closing fills
    must add up. When they do not we are missing fills, and the mid is
    the honest fallback — blending a real fill with a guess would
    produce a number nobody can audit. Nothing is consumed on a miss, so
    the partial stays available for a smaller row.

    `commit=False` leaves `remaining` untouched (a pure summary).
    """
    if size <= 0:
        return None
    available = sum(e.remaining for e in entries)
    tolerance = max(size * 1e-9, _COVER_EPS)
    if available < size - tolerance:
        return None

    remaining = float(size)
    notional = 0.0
    consumed = 0.0
    fee_total = 0.0
    order_id: str | None = None
    taken: list[tuple[_LedgerEntry, float]] = []
    for e in entries:
        if remaining <= _COVER_EPS:
            break
        if e.remaining <= 0:
            continue
        take = min(e.remaining, remaining)
        notional += e.price * take
        fee_total += e.fee * (take / e.size)
        consumed += take
        remaining -= take
        taken.append((e, take))
        if order_id is None:
            order_id = e.order_id
    if consumed <= 0:
        return None
    if commit:
        for entry, take in taken:
            entry.remaining -= take
    return ClosingFillSummary(
        price=notional / consumed,
        size=consumed,
        fee=fee_total,
        order_id=order_id,
    )


class FillLedger:
    """The pass's fills, each spendable exactly once.

    Two DB rows on the same coin and side used to be priced from the
    SAME fill — one close, two `Trade` rows, the fee booked twice, and
    an oid collision papered over by the savepoint fallback. The ledger
    makes a fill's size a finite resource: whichever row takes it first
    gets it, and a row the remainder cannot cover falls back to the mid.

    `exclude_order_ids` removes fills already booked as `trades` rows
    before anything is spendable — another strategy's signal-path close
    is not this orphan's close.
    """

    def __init__(
        self,
        fills: list[dict],
        *,
        exclude_order_ids: frozenset[str] | set[str] | None = None,
    ) -> None:
        excluded = frozenset(exclude_order_ids or ())
        parsed = _parse_entries(fills)
        self._entries = [
            e for e in parsed
            if e.order_id is None or e.order_id not in excluded
        ]
        self.excluded_count = len(parsed) - len(self._entries)
        self.usable_count = len(self._entries)

    def take(
        self,
        *,
        symbol: str,
        position_side: str,
        size: float,
        since_ms: int | None = None,
    ) -> ClosingFillSummary | None:
        """Spend `size` of unspent fills that close `position_side`."""
        want = closing_side(position_side)
        candidates = [
            e for e in self._entries
            if e.coin == symbol
            and e.side == want
            and e.remaining > 0
            and (since_ms is None or e.time_ms >= since_ms)
        ]
        return _consume(candidates, size, commit=True)


def summarize_closing_fills(
    fills: list[dict], *, size: float,
) -> ClosingFillSummary | None:
    """Non-consuming price + fee for an already-filtered fill list.

    Same rules as `FillLedger.take` (full coverage or nothing), without
    the spend. Kept as the pure, directly testable form of the maths.
    """
    return _consume(_parse_entries(fills), size, commit=False)


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
