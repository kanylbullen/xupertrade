"""Tests for Repository tagging INSERTs with tenant_id (multi-tenancy
Phase 3b).

When the Repository is constructed with a tenant_id (or settings.tenant_id
is set via TENANT_ID env), every Trade / PositionRecord / EquitySnapshot /
FundingPayment INSERT carries that tenant_id. When tenant_id is None,
the rows write tenant_id=NULL — back-compat for the operator's
current 3-mode deploy until Phase 6 cutover backfills + flips the
column to NOT NULL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from hypertrade.db.repo import Repository
from hypertrade.db import models


TENANT_UUID = "abc12345-aaaa-bbbb-cccc-111122223333"


@pytest.fixture
async def tenanted_repo():
    """Repository scoped to a fixed tenant_id."""
    repo = Repository(
        "sqlite+aiosqlite:///:memory:",
        tenant_id=TENANT_UUID,
    )
    repo._mode = "paper"
    repo._is_paper = True
    await repo.init_db()
    try:
        yield repo
    finally:
        await repo._engine.dispose()


@pytest.fixture
async def untenanted_repo():
    """Repository with no tenant_id — emulates operator's pre-cutover deploy."""
    repo = Repository("sqlite+aiosqlite:///:memory:", tenant_id=None)
    repo._mode = "testnet"
    repo._is_paper = False
    await repo.init_db()
    try:
        yield repo
    finally:
        await repo._engine.dispose()


@pytest.mark.asyncio
async def test_record_trade_tags_tenant_id(tenanted_repo):
    repo = tenanted_repo
    trade = await repo.record_trade(
        order_id="t-1", strategy_name="bb_short", symbol="SOL",
        side="buy", size=1.0, price=100.0,
    )
    assert trade.tenant_id == repo._tenant_id


@pytest.mark.asyncio
async def test_open_position_tags_tenant_id(tenanted_repo):
    repo = tenanted_repo
    pos = await repo.open_position(
        strategy_name="bb_short", symbol="SOL",
        side="long", size=1.0, entry_price=100.0,
    )
    assert pos.tenant_id == repo._tenant_id


@pytest.mark.asyncio
async def test_atomic_open_tags_both_rows(tenanted_repo):
    repo = tenanted_repo
    trade, pos = await repo.record_trade_and_open_position(
        order_id="t-2", strategy_name="bb_short", symbol="SOL",
        trade_side="buy", position_side="long",
        size=1.0, price=100.0,
    )
    assert trade.tenant_id == repo._tenant_id
    assert pos.tenant_id == repo._tenant_id


@pytest.mark.asyncio
async def test_snapshot_equity_tags_tenant_id(tenanted_repo):
    repo = tenanted_repo
    await repo.snapshot_equity(total=10000.0, available=5000.0, unrealized_pnl=0.0)
    async with repo._session_factory() as session:
        rows = (await session.scalars(
            select(models.EquitySnapshot)
        )).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == repo._tenant_id


@pytest.mark.asyncio
async def test_funding_payment_tags_tenant_id(tenanted_repo):
    repo = tenanted_repo
    inserted = await repo.upsert_funding_payment(
        ts=datetime.now(timezone.utc),
        h="hash-1",
        coin="SOL",
        usdc=-0.05,
        szi=1.0,
        funding_rate=0.0001,
        strategy_name="bb_short",
    )
    assert inserted is True
    async with repo._session_factory() as session:
        rows = (await session.scalars(
            select(models.FundingPayment)
        )).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == repo._tenant_id


@pytest.mark.asyncio
async def test_record_trade_with_no_tenant_writes_null(untenanted_repo):
    """Operator's pre-cutover deploy still writes rows with NULL
    tenant_id. Phase 6 backfills these to operator's tenant 1 row."""
    repo = untenanted_repo
    trade = await repo.record_trade(
        order_id="legacy-1", strategy_name="bb_short", symbol="SOL",
        side="buy", size=1.0, price=100.0,
    )
    assert trade.tenant_id is None


@pytest.mark.asyncio
async def test_constructor_falls_back_to_settings_tenant_id(monkeypatch):
    """If no explicit tenant_id, Repository reads settings.tenant_id
    (which is wired to the TENANT_ID env var). This is what the bot
    process uses in production."""
    from hypertrade.config import settings
    env_uuid = "deadbeef-1111-2222-3333-444455556666"
    monkeypatch.setattr(settings, "tenant_id", env_uuid)
    repo = Repository("sqlite+aiosqlite:///:memory:")  # no explicit tenant_id arg
    assert repo._tenant_id == uuid.UUID(env_uuid)
    await repo._engine.dispose()


@pytest.mark.asyncio
async def test_constructor_with_invalid_uuid_string_raises():
    """Garbage in TENANT_ID is a deploy-config bug — fail fast at
    Repository construction rather than silently mis-tagging rows."""
    with pytest.raises(ValueError, match="badly formed"):
        Repository(
            "sqlite+aiosqlite:///:memory:",
            tenant_id="not-a-uuid",
        )
