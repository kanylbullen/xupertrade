"""Tests for the atomic Trade + PositionRecord methods (audit M8).

Pre-fix the runner did `record_trade()` then `open_position()` in two
separate sessions — a SIGTERM between them left a Trade row with no
matching PositionRecord, which the next reconcile loop treated as an
exchange-side orphan and force-closed. The atomic methods commit both
inserts in one transaction so that gap can't open.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hypertrade.db import models
from hypertrade.db.repo import Repository


@pytest.fixture
async def in_memory_repo():
    """Repository pointed at a fresh in-memory SQLite DB.

    Bypasses the settings-driven URL by passing it to the constructor;
    `_mode` / `_is_paper` are overwritten directly afterward since they
    derive from settings at __init__ time.
    """
    repo = Repository("sqlite+aiosqlite:///:memory:")
    repo._mode = "paper"
    repo._is_paper = True
    await repo.init_db()
    yield repo
    await repo._engine.dispose()


@pytest.mark.asyncio
async def test_record_trade_and_open_position_writes_both(in_memory_repo):
    """Happy path: both Trade and PositionRecord land in DB."""
    repo = in_memory_repo
    trade, pos = await repo.record_trade_and_open_position(
        order_id="test-1",
        strategy_name="ath_breakout",
        symbol="BTC",
        trade_side="buy",
        position_side="long",
        size=0.01,
        price=50000.0,
        fee=0.225,
        reason="test entry",
    )
    assert trade.id is not None
    assert pos.id is not None
    assert pos.is_open is True
    # Verify both rows are readable from the DB (was a dead-code
    # `hasattr(...)` branch that never asserted anything; review fix).
    open_pos = await repo.get_open_position("ath_breakout", "BTC")
    assert open_pos is not None
    assert open_pos.entry_price == 50000.0
    async with repo._session_factory() as session:
        result = await session.execute(
            select(models.Trade).where(models.Trade.order_id == "test-1")
        )
        rows = list(result.scalars().all())
    assert len(rows) == 1
    assert rows[0].fee == 0.225


@pytest.mark.asyncio
async def test_record_trade_and_open_position_atomic_on_failure(in_memory_repo):
    """If the position insert fails for any reason, the trade insert
    must roll back too (otherwise we get the orphan-Trade scenario the
    audit M8 fix is designed to prevent)."""
    repo = in_memory_repo

    # Force a failure by passing an invalid position_side that violates
    # nothing in the model directly — instead, monkey-patch the session
    # to raise on the second add. Easiest portable way: patch
    # PositionRecord.__init__ to raise on any call after a flag is set.
    original_init = models.PositionRecord.__init__
    call_count = {"n": 0}

    def _failing_init(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated PositionRecord failure")
        original_init(self, *args, **kwargs)

    models.PositionRecord.__init__ = _failing_init
    try:
        with pytest.raises(RuntimeError):
            await repo.record_trade_and_open_position(
                order_id="rollback-test",
                strategy_name="testfail",
                symbol="BTC",
                trade_side="buy",
                position_side="long",
                size=0.01,
                price=50000.0,
            )
    finally:
        models.PositionRecord.__init__ = original_init

    # Trade row must NOT exist (atomic rollback)
    async with repo._session_factory() as session:
        result = await session.execute(
            select(models.Trade).where(models.Trade.order_id == "rollback-test")
        )
        rows = list(result.scalars().all())
    assert rows == [], (
        "Trade row leaked after PositionRecord failure — "
        "atomicity broken (audit M8 regression)"
    )


@pytest.mark.asyncio
async def test_record_trade_and_close_position_updates_existing(in_memory_repo):
    """Open then close via the atomic methods. Position must flip to
    is_open=False with the close fields set, and a Trade row exists."""
    repo = in_memory_repo
    await repo.record_trade_and_open_position(
        order_id="open-1",
        strategy_name="s1",
        symbol="ETH",
        trade_side="buy",
        position_side="long",
        size=0.5,
        price=2000.0,
    )
    trade = await repo.record_trade_and_close_position(
        order_id="close-1",
        strategy_name="s1",
        symbol="ETH",
        trade_side="sell",
        size=0.5,
        price=2100.0,
        fee=0.45,
        pnl=49.55,
        reason="TP hit",
    )
    assert trade is not None
    assert trade.pnl == 49.55
    open_pos = await repo.get_open_position("s1", "ETH")
    assert open_pos is None, "position should be closed after close-trade"


@pytest.mark.asyncio
async def test_record_trade_and_close_position_no_matching_pos(in_memory_repo):
    """Close-trade without a matching open position still records the
    trade (defensive — caller may have a reason). Returns Trade with
    pos=None semantics (no UPDATE happened)."""
    repo = in_memory_repo
    trade = await repo.record_trade_and_close_position(
        order_id="orphan-close",
        strategy_name="ghost",
        symbol="DOGE",
        trade_side="sell",
        size=10.0,
        price=0.1,
        pnl=0.0,
        reason="no-op close",
    )
    assert trade is not None
    assert trade.order_id == "orphan-close"
