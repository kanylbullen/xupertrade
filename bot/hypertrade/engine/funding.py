"""Funding ingest: HyperLiquid `userFunding` → `funding_payments` (NU-6.1),
shared by the runner's 30-minute poll and `reports/funding_backfill.py`.

- **Key:** (tenant, mode, coin, timestamp). HL settles funding once per
  coin per hour per account; its `hash` is all zeros (alembic 0017).
- **Paging:** HL answers at most 500 events, oldest first. After a full
  page the next request starts at its newest timestamp, inclusive, so
  events sharing that millisecond are read twice and deduped, not lost.
- **Attribution:** to the position (same tenant, mode, coin) whose
  [opened_at, closed_at) covers the event; if several, the one whose
  side matches the sign of `szi`; if that is not exactly one, nobody.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PAGE_CAP = 500  # HL's cap on events per `userFunding` answer
# The poll runs inside the tick, and a full page weighs 45 of the IP's
# 1200/min, shared with every bot's candle reads. 2000 events is far
# above one per held coin per hour; a longer backlog takes several polls.
POLL_MAX_PAGES = 4

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS = timedelta(milliseconds=1)

# fetch(start_ms, end_ms) → one page of raw `userFunding` records.
FetchFunding = Callable[[int, int | None], Awaitable[list[dict]]]


def as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything stored is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def to_ms(value: datetime) -> int:
    """Epoch milliseconds, exactly — no float round-trip, so an event's
    key read back from the DB equals the key it was written with."""
    return (as_utc(value) - _EPOCH) // _MS


def from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


@dataclass(frozen=True)
class FundingEvent:
    ts: datetime
    coin: str
    usdc: float
    szi: float | None
    funding_rate: float | None
    hash: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.coin, to_ms(self.ts))


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)


def parse_event(record: object) -> FundingEvent | None:
    """One `userFunding` record, or None when it is not a funding event
    that can be stored (no coin, no time, a non-numeric amount)."""
    try:
        delta = record.get("delta") or {}
        if delta.get("type", "funding") != "funding":
            return None
        coin = str(delta.get("coin") or "")
        ts_ms = int(record["time"])
        event = FundingEvent(
            ts=from_ms(ts_ms),
            coin=coin,
            usdc=float(delta["usdc"]),
            szi=_opt_float(delta.get("szi")),
            funding_rate=_opt_float(delta.get("fundingRate")),
            hash=str(record.get("hash") or ""),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if not coin or ts_ms <= 0:
        return None
    return event


def _covers(position, ts: datetime) -> bool:
    """Held at `ts`: [opened_at, closed_at). A closed row without a
    closed_at has no known end and covers nothing."""
    if position.opened_at is None or as_utc(position.opened_at) > ts:
        return False
    if position.closed_at is None:
        return bool(position.is_open)
    return ts < as_utc(position.closed_at)


def attribute(positions: Iterable, event: FundingEvent):
    """The position `event` was paid on (module docstring), or None.
    `positions` are this tenant's and mode's rows."""
    covering = [
        p for p in positions
        if p.symbol == event.coin and _covers(p, event.ts)
    ]
    if len(covering) == 1:
        return covering[0]
    if len(covering) > 1 and event.szi:
        side = "long" if event.szi > 0 else "short"
        matching = [p for p in covering if p.side == side]
        if len(matching) == 1:
            return matching[0]
    return None


@dataclass
class IngestResult:
    pages: int = 0
    events: int = 0            # distinct funding events read
    skipped: int = 0           # records that were not storable events
    new: int = 0               # inserted, or missing on a dry run
    total_usdc: float = 0.0    # over every event read
    new_usdc: float = 0.0
    unattributed_new: int = 0
    by_coin: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    # Over the new events; None is "no position".
    by_strategy: dict[str | None, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    newest: datetime | None = None
    # False when paging stopped before HL ran out of events.
    complete: bool = True


async def _ingest_page(
    repo, page: list, seen: set[tuple[str, int]], result: IngestResult, apply: bool,
) -> int:
    """Store (or count) one page; returns its newest event time in ms."""
    events: list[FundingEvent] = []
    newest_ms = 0
    for record in page:
        event = parse_event(record)
        if event is None:
            result.skipped += 1
            continue
        newest_ms = max(newest_ms, event.key[1])
        if event.key not in seen:  # else: re-read at the page boundary
            seen.add(event.key)
            events.append(event)
    if not events:
        return newest_ms

    lo = min(e.ts for e in events)
    hi = max(e.ts for e in events)
    positions = await repo.get_positions_covering({e.coin for e in events}, lo, hi)
    owner: dict[tuple[str, int], str | None] = {}
    rows = []
    for event in events:
        position = attribute(positions, event)
        owner[event.key] = position.strategy_name if position else None
        rows.append({
            "timestamp": event.ts,
            "hash": event.hash,
            "coin": event.coin,
            "usdc": event.usdc,
            "szi": event.szi,
            "funding_rate": event.funding_rate,
            "strategy_name": owner[event.key],
        })
        result.events += 1
        result.total_usdc += event.usdc
        result.by_coin[event.coin] += event.usdc

    if apply:
        written = await repo.insert_funding_payments(rows)
        new_keys = {(coin, to_ms(ts)) for coin, ts in written}
    else:
        stored = await repo.get_funding_event_keys(lo, hi)
        new_keys = {e.key for e in events} - {
            (coin, to_ms(ts)) for coin, ts in stored
        }

    for event in events:
        if event.key not in new_keys:
            continue
        strategy = owner[event.key]
        result.new += 1
        result.new_usdc += event.usdc
        result.by_strategy[strategy] += event.usdc
        if strategy is None:
            result.unattributed_new += 1
    result.newest = hi if result.newest is None else max(result.newest, hi)
    return newest_ms


async def ingest_funding(
    repo,
    fetch: FetchFunding,
    start_ms: int,
    end_ms: int | None = None,
    *,
    apply: bool = True,
    max_pages: int | None = None,
    page_pause: float = 0.0,
) -> IngestResult:
    """Read funding events from `start_ms` (to `end_ms`, or now) and
    store the ones `repo` lacks — or, with `apply=False`, only count
    them. Pages forward while HL answers full pages, up to `max_pages`,
    sleeping `page_pause` seconds between requests."""
    result = IngestResult()
    seen: set[tuple[str, int]] = set()
    cursor = start_ms
    while True:
        page = await fetch(cursor, end_ms)
        if not page:
            break
        result.pages += 1
        newest = await _ingest_page(repo, page, seen, result, apply)
        if len(page) < PAGE_CAP:
            break
        if newest <= cursor:
            # A full page inside one millisecond: repeating it would loop,
            # skipping past it would lose what it cut off. Stop and say.
            logger.warning(
                "Funding: %d events at one timestamp (%d ms); cannot page past it",
                len(page), cursor,
            )
            result.complete = False
            break
        if max_pages is not None and result.pages >= max_pages:
            result.complete = False
            break
        cursor = newest
        if page_pause:
            await asyncio.sleep(page_pause)
    return result
