"""Funding ingest (roadmap NU-6.1): key, paging, attribution.

Before this, `funding_payments.hash` was UNIQUE and HyperLiquid sends the
same all-zero hash for every `userFunding` event, so one funding row was
ever stored (2026-04-28); and the poller read one page of at most 500
events. `engine/funding.py` keys an event on (tenant, mode, coin, time),
pages forward while HL answers full pages, and attributes each event to
the position that covered it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.engine import funding
from hypertrade.engine.runner import EngineRunner

TENANT = "abc12345-aaaa-bbbb-cccc-111122223333"
OTHER_TENANT = "def67890-aaaa-bbbb-cccc-444455556666"
ZERO_HASH = "0x" + "0" * 64
T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
T0_MS = funding.to_ms(T0)
HOUR_MS = 3_600_000


def record(ms: int, coin: str = "BTC", usdc: float = -0.5, szi: float | None = 0.01):
    """One HL `userFunding` record, as the API sends it."""
    delta = {"type": "funding", "coin": coin, "usdc": str(usdc),
             "fundingRate": "0.0000125", "nSamples": None}
    if szi is not None:
        delta["szi"] = str(szi)
    return {"time": ms, "hash": ZERO_HASH, "delta": delta}


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:", tenant_id=TENANT, mode="testnet")
    await r.init_db()
    yield r
    await r._engine.dispose()


async def stored(repo: Repository) -> list[models.FundingPayment]:
    async with repo._session_factory() as session:
        return list((await session.scalars(
            select(models.FundingPayment).order_by(models.FundingPayment.timestamp)
        )).all())


async def add_position(repo, strategy, *, side="long", coin="BTC", opened=T0,
                       closed=None, is_open=None, tenant=TENANT, mode="testnet"):
    async with repo._session_factory() as session:
        session.add(models.PositionRecord(
            tenant_id=uuid.UUID(tenant), strategy_name=strategy, symbol=coin,
            side=side, size=0.01, entry_price=100.0, mode=mode, is_paper=False,
            is_open=(closed is None) if is_open is None else is_open,
            opened_at=opened, closed_at=closed,
        ))
        await session.commit()


async def store_one(repo: Repository, ts: datetime, coin: str = "BTC"):
    return await repo.insert_funding_payments([{
        "timestamp": ts, "hash": ZERO_HASH, "coin": coin, "usdc": -0.5,
        "szi": 0.01, "funding_rate": None, "strategy_name": None,
    }])


def pager(pages: list[list[dict]]):
    """fetch() that answers `pages` in order, then nothing."""
    calls: list[tuple[int, int | None]] = []
    queue = list(pages)

    async def fetch(start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return queue.pop(0) if queue else []

    return fetch, calls


# ── the key ──────────────────────────────────────────────────────────


async def test_two_events_with_all_zero_hashes_are_both_stored(repo):
    fetch, _ = pager([[record(T0_MS), record(T0_MS + HOUR_MS)]])

    result = await funding.ingest_funding(repo, fetch, T0_MS)

    rows = await stored(repo)
    assert [r.hash for r in rows] == [ZERO_HASH, ZERO_HASH]
    assert [funding.to_ms(r.timestamp) for r in rows] == [T0_MS, T0_MS + HOUR_MS]
    assert result.new == 2


async def test_the_same_event_is_stored_once(repo):
    fetch, _ = pager([[record(T0_MS)], [record(T0_MS)]])

    first = await funding.ingest_funding(repo, fetch, T0_MS)
    second = await funding.ingest_funding(repo, fetch, T0_MS)

    assert (first.new, second.new) == (1, 0)
    assert second.events == 1
    assert len(await stored(repo)) == 1


async def test_same_time_on_two_coins_or_two_modes_are_two_events(repo):
    fetch, _ = pager([[record(T0_MS, "BTC"), record(T0_MS, "ETH")]])
    await funding.ingest_funding(repo, fetch, T0_MS)
    repo._mode = "mainnet"

    assert len(await store_one(repo, T0)) == 1
    assert len(await stored(repo)) == 3


def test_parse_event_rejects_what_it_cannot_store():
    assert funding.parse_event(record(T0_MS)).key == ("BTC", T0_MS)
    assert funding.parse_event({"time": T0_MS, "delta": {"usdc": "1"}}) is None
    assert funding.parse_event({"time": T0_MS, "delta": {
        "type": "deposit", "coin": "BTC", "usdc": "1"}}) is None
    assert funding.parse_event({"time": "x", "delta": {"coin": "BTC", "usdc": "1"}}) is None
    assert funding.parse_event("not a record") is None
    # A missing hash is stored as "", not skipped: the hash is not the key.
    no_hash = {"time": T0_MS, "delta": {"coin": "BTC", "usdc": "1"}}
    assert funding.parse_event(no_hash).hash == ""


# ── paging ───────────────────────────────────────────────────────────


def full_page(first_ms: int, coins=("BTC", "ETH", "SOL", "HYPE", "DOGE")):
    """500 events, five coins an hour, oldest first."""
    per_hour = len(coins)
    return [
        record(first_ms + (i // per_hour) * HOUR_MS, coins[i % per_hour])
        for i in range(funding.PAGE_CAP)
    ]


async def test_pages_past_500_from_the_newest_timestamp(repo):
    page1 = full_page(T0_MS)
    newest = page1[-1]["time"]
    # HL's next page starts at `newest` inclusive, so it repeats the
    # events of that hour before the new ones.
    page2 = [r for r in page1 if r["time"] == newest] + [
        record(newest + HOUR_MS, "BTC"), record(newest + HOUR_MS, "ETH"),
    ]
    fetch, calls = pager([page1, page2])

    result = await funding.ingest_funding(repo, fetch, T0_MS)

    assert [c[0] for c in calls] == [T0_MS, newest]
    assert result.pages == 2
    assert result.new == result.events == funding.PAGE_CAP + 2
    assert len(await stored(repo)) == funding.PAGE_CAP + 2
    assert result.complete


async def test_a_short_first_page_is_the_only_request(repo):
    fetch, calls = pager([[record(T0_MS)], [record(T0_MS + HOUR_MS)]])
    await funding.ingest_funding(repo, fetch, T0_MS)
    assert len(calls) == 1


async def test_max_pages_stops_and_the_next_poll_resumes(repo):
    page1 = full_page(T0_MS)
    page2 = full_page(page1[-1]["time"] + HOUR_MS)
    fetch, calls = pager([page1, page2, [record(page2[-1]["time"] + HOUR_MS)]])

    first = await funding.ingest_funding(repo, fetch, T0_MS, max_pages=1)
    assert (first.pages, first.complete) == (1, False)
    latest = await repo.get_latest_funding_timestamp()
    assert funding.to_ms(latest) == page1[-1]["time"]

    second = await funding.ingest_funding(repo, fetch, funding.to_ms(latest))
    assert second.complete
    assert len(await stored(repo)) == 2 * funding.PAGE_CAP + 1


async def test_a_full_page_inside_one_millisecond_stops_instead_of_looping(repo, caplog):
    same_ms = [record(T0_MS, f"C{i}") for i in range(funding.PAGE_CAP)]
    fetch, calls = pager([same_ms, same_ms, same_ms])

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.funding"):
        result = await funding.ingest_funding(repo, fetch, T0_MS)

    assert len(calls) == 1
    assert not result.complete
    assert "cannot page past it" in caplog.text


# ── attribution ──────────────────────────────────────────────────────


def pos(strategy, side="long", opened=T0 - timedelta(hours=1), closed=None,
        coin="BTC", is_open=None):
    return SimpleNamespace(
        strategy_name=strategy, side=side, symbol=coin, opened_at=opened,
        closed_at=closed, is_open=(closed is None) if is_open is None else is_open,
    )


def event(szi=0.01, coin="BTC", ts=T0):
    return funding.FundingEvent(ts=ts, coin=coin, usdc=-1.0, szi=szi,
                                funding_rate=None, hash=ZERO_HASH)


def test_the_one_covering_position_gets_the_event():
    held = pos("held")
    assert funding.attribute([held], event()) is held
    # One covering row is the answer even against szi's sign: HL nets per
    # coin, and the row is the only one that could be paying.
    assert funding.attribute([held], event(szi=-0.01)) is held


def test_the_covering_interval_is_half_open():
    closed_at_event = pos("closed", closed=T0)
    opened_at_event = pos("opened", opened=T0)
    other_coin = pos("eth", coin="ETH")
    opened_later = pos("later", opened=T0 + timedelta(seconds=1))
    assert funding.attribute([closed_at_event, other_coin, opened_later], event()) is None
    assert funding.attribute([closed_at_event, opened_at_event], event()) is opened_at_event


def test_a_closed_row_without_closed_at_covers_nothing():
    assert funding.attribute([pos("legacy", is_open=False)], event()) is None


def test_overlapping_positions_go_to_the_side_of_szi():
    long_, short = pos("longer", "long"), pos("shorter", "short")
    assert funding.attribute([long_, short], event(szi=0.02)) is long_
    assert funding.attribute([long_, short], event(szi=-0.02)) is short


def test_overlapping_positions_on_one_side_stay_unattributed():
    a, b = pos("a", "long"), pos("b", "long")
    assert funding.attribute([a, b], event(szi=0.02)) is None
    assert funding.attribute([pos("x", "long"), pos("y", "short")], event(szi=None)) is None


async def test_ingest_attributes_by_event_time_not_by_what_is_open_now(repo):
    # Held and closed before the poll ran: the old poller asked "who holds
    # BTC now" and found nobody (or the wrong strategy).
    await add_position(repo, "was_long", side="long",
                       opened=T0 - timedelta(hours=2), closed=T0 + timedelta(minutes=30))
    await add_position(repo, "was_short", side="short",
                       opened=T0 - timedelta(hours=1), closed=T0 + timedelta(minutes=30))
    await add_position(repo, "now_open", opened=T0 + timedelta(hours=2))
    await add_position(repo, "other_tenant", opened=T0 - timedelta(hours=3),
                       tenant=OTHER_TENANT)
    fetch, _ = pager([[
        record(T0_MS, szi=-0.01),                   # both cover → short side
        record(T0_MS + HOUR_MS, szi=0.01),          # nobody covers
        record(T0_MS + 3 * HOUR_MS, szi=0.01),      # now_open
    ]])

    result = await funding.ingest_funding(repo, fetch, T0_MS)

    assert [r.strategy_name for r in await stored(repo)] == [
        "was_short", None, "now_open",
    ]
    assert result.unattributed_new == 1


# ── dry run and the runner ───────────────────────────────────────────


async def test_dry_run_counts_missing_events_and_writes_nothing(repo):
    await store_one(repo, T0)
    fetch, _ = pager([[record(T0_MS), record(T0_MS + HOUR_MS, usdc=-0.25)]])

    result = await funding.ingest_funding(repo, fetch, T0_MS, apply=False)

    assert (result.events, result.new) == (2, 1)
    assert result.new_usdc == pytest.approx(-0.25)
    assert result.total_usdc == pytest.approx(-0.75)
    assert len(await stored(repo)) == 1


async def test_runner_poll_resumes_a_minute_before_the_newest_row_and_pages(repo):
    await store_one(repo, T0)
    start = T0_MS - 60_000
    page1 = full_page(start + 60_000)
    fetch, calls = pager([page1, [record(page1[-1]["time"] + HOUR_MS)]])
    exchange = MagicMock()
    exchange.get_user_funding_history = AsyncMock(side_effect=fetch)
    runner = EngineRunner(exchange=exchange, strategies=[], repo=repo,
                          event_bus=None, control=MagicMock())

    await runner._poll_funding()

    assert [c[0] for c in calls] == [start, page1[-1]["time"]]
    # 500 on page 1 (its first event is the stored one) plus 1 on page 2.
    assert len(await stored(repo)) == funding.PAGE_CAP + 1
