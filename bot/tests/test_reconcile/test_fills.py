"""Tests for reconcile_fills_from_hl: backfilling missing trade rows
from HL fill history (incident PR #107 follow-up)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.exchange.base import Exchange
from hypertrade.reconcile import reconcile_fills_from_hl


class FakeExchange(Exchange):
    def __init__(self, fills: list[dict]) -> None:
        self._fills = fills

    async def fetch_user_fills(
        self, address: str | None = None, since_ms: int | None = None,
    ) -> list[dict]:
        return list(self._fills)

    async def place_order(self, *a, **kw):
        raise NotImplementedError

    async def cancel_order(self, *a, **kw):
        raise NotImplementedError

    async def get_positions(self):
        return []

    async def get_position(self, *a, **kw):
        return None

    async def get_balance(self):
        raise NotImplementedError

    async def get_current_price(self, *a, **kw):
        return 0.0


@pytest.fixture
async def in_memory_repo():
    repo = Repository("sqlite+aiosqlite:///:memory:")
    repo._mode = "testnet"
    repo._is_paper = False
    await repo.init_db()
    yield repo
    await repo._engine.dispose()


def _fill(oid: int, coin: str = "BTC", side: str = "B", sz: float = 0.1,
          px: float = 50000.0, fee: float = 0.5, pnl: str = "0",
          time_ms: int = 1700000000000) -> dict:
    return {
        "oid": oid,
        "coin": coin,
        "side": side,
        "sz": str(sz),
        "px": str(px),
        "fee": str(fee),
        "closedPnl": pnl,
        "time": time_ms,
    }


@pytest.mark.asyncio
async def test_inserts_missing_fills(in_memory_repo):
    repo = in_memory_repo
    fills = [_fill(1), _fill(2, side="A", pnl="12.5")]
    ex = FakeExchange(fills)

    report = await reconcile_fills_from_hl(exchange=ex, repo=repo)

    assert report.examined == 2
    assert report.inserted == 2
    assert report.skipped == 0
    assert len(report.inserted_ids) == 2

    async with repo._session_factory() as session:
        rows = list(
            (await session.execute(select(models.Trade))).scalars().all()
        )
    assert len(rows) == 2
    by_oid = {r.order_id: r for r in rows}
    assert by_oid["1"].strategy_name == "reconciled"
    assert by_oid["1"].side == "buy"
    assert by_oid["1"].mode == "testnet"
    assert by_oid["1"].is_paper is False
    assert by_oid["2"].side == "sell"
    assert by_oid["2"].pnl == 12.5


@pytest.mark.asyncio
async def test_dedup_skips_existing(in_memory_repo):
    repo = in_memory_repo
    await repo.record_trade(
        order_id="42",
        strategy_name="ema_crossover",
        symbol="BTC", side="buy", size=0.1, price=50000.0,
    )
    ex = FakeExchange([_fill(42), _fill(43)])

    report = await reconcile_fills_from_hl(exchange=ex, repo=repo)

    assert report.examined == 2
    assert report.inserted == 1
    assert report.skipped == 1

    async with repo._session_factory() as session:
        rows = list(
            (await session.execute(
                select(models.Trade).order_by(models.Trade.order_id)
            )).scalars().all()
        )
    assert [r.order_id for r in rows] == ["42", "43"]
    assert rows[0].strategy_name == "ema_crossover"
    assert rows[1].strategy_name == "reconciled"


@pytest.mark.asyncio
async def test_all_existing_inserts_zero(in_memory_repo):
    repo = in_memory_repo
    for oid in (1, 2, 3):
        await repo.record_trade(
            order_id=str(oid),
            strategy_name="ema_crossover",
            symbol="BTC", side="buy", size=0.1, price=50000.0,
        )
    ex = FakeExchange([_fill(1), _fill(2), _fill(3)])

    report = await reconcile_fills_from_hl(exchange=ex, repo=repo)

    assert report.examined == 3
    assert report.inserted == 0
    assert report.skipped == 3
    assert report.inserted_ids == []


@pytest.mark.asyncio
async def test_empty_fills_is_noop(in_memory_repo):
    repo = in_memory_repo
    ex = FakeExchange([])

    report = await reconcile_fills_from_hl(exchange=ex, repo=repo)

    assert report.examined == 0
    assert report.inserted == 0
    assert report.skipped == 0


@pytest.mark.asyncio
async def test_dedup_is_string_match_on_oid(in_memory_repo):
    """oid comes back as int from HL; we store as string. Make sure
    dedup compares string-to-string so an existing '7' matches int 7."""
    repo = in_memory_repo
    await repo.record_trade(
        order_id="7",
        strategy_name="ema_crossover",
        symbol="ETH", side="sell", size=1.0, price=3000.0,
    )
    ex = FakeExchange([_fill(7, coin="ETH", side="A")])

    report = await reconcile_fills_from_hl(exchange=ex, repo=repo)

    assert report.examined == 1
    assert report.inserted == 0
    assert report.skipped == 1
