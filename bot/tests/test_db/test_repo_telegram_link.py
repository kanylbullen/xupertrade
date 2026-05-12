"""Tests for Repository.upsert_telegram_link semantics (PR 3b).

The schema (alembic 0013) requires telegram_chat_id to be globally
unique. upsert_telegram_link evicts the prior owner of a chat_id
before inserting/updating, so a chat that gets re-linked to a new
tenant transfers cleanly without violating the UNIQUE constraint.

Init_db doesn't create the tenant_telegram_links table on its own —
it's alembic-owned. Tests that need the table either use a real
postgres or skip. We use the SAVAlchemy `create_all(tables=[...])`
escape hatch to materialize just this one table on the in-memory
SQLite fixture.
"""

from __future__ import annotations

import uuid

import pytest

from hypertrade.db import models
from hypertrade.db.repo import Repository


@pytest.fixture
async def repo_with_telegram_table():
    repo = Repository("sqlite+aiosqlite:///:memory:", tenant_id=None)
    await repo.init_db()
    # Materialize the alembic-owned table for this test session
    # only. init_db() skips it on purpose; we add it explicitly
    # because we're exercising its semantics.
    async with repo._engine.begin() as conn:
        await conn.run_sync(
            lambda c: models.Base.metadata.create_all(
                c, tables=[models.TenantTelegramLink.__table__]
            )
        )
    # Also need the tenants table for the FK.
    async with repo._engine.begin() as conn:
        await conn.run_sync(
            lambda c: models.Base.metadata.create_all(
                c, tables=[models.Tenant.__table__]
            )
        )
    try:
        yield repo
    finally:
        await repo._engine.dispose()


async def _seed_tenant(repo: Repository, tid: uuid.UUID, email: str):
    """Insert a minimal tenants row so the FK on
    tenant_telegram_links is satisfied."""
    async with repo._session_factory() as session:
        session.add(
            models.Tenant(
                id=tid,
                authentik_sub=f"sub-{email}",
                email=email,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_upsert_inserts_new_link(repo_with_telegram_table):
    repo = repo_with_telegram_table
    tid = uuid.uuid4()
    await _seed_tenant(repo, tid, "alice@example.com")

    await repo.upsert_telegram_link(
        tenant_id=tid,
        telegram_chat_id=111,
        telegram_username="alice",
    )

    found = await repo.get_tenant_id_for_telegram_chat(111)
    assert found == tid


@pytest.mark.asyncio
async def test_upsert_updates_same_tenants_chat(repo_with_telegram_table):
    repo = repo_with_telegram_table
    tid = uuid.uuid4()
    await _seed_tenant(repo, tid, "alice@example.com")

    # First link
    await repo.upsert_telegram_link(
        tenant_id=tid, telegram_chat_id=111, telegram_username="alice",
    )
    # Re-link the same tenant to a new chat (e.g. switched phones)
    await repo.upsert_telegram_link(
        tenant_id=tid, telegram_chat_id=222, telegram_username="alice2",
    )

    # Old chat no longer maps to anyone
    assert await repo.get_tenant_id_for_telegram_chat(111) is None
    # New chat maps to same tenant
    assert await repo.get_tenant_id_for_telegram_chat(222) == tid


@pytest.mark.asyncio
async def test_upsert_evicts_stale_owner_of_same_chat(repo_with_telegram_table):
    """Re-linking a chat that ALREADY belongs to a different tenant
    transfers it. The previous owner's row is deleted to satisfy
    the UNIQUE constraint on telegram_chat_id (alembic 0013)."""
    repo = repo_with_telegram_table
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    await _seed_tenant(repo, alice, "alice@example.com")
    await _seed_tenant(repo, bob, "bob@example.com")

    # Alice links chat 999
    await repo.upsert_telegram_link(
        tenant_id=alice, telegram_chat_id=999, telegram_username="alice",
    )
    assert await repo.get_tenant_id_for_telegram_chat(999) == alice

    # Bob links the SAME chat (e.g. inherited the chat after Alice
    # deleted her account and Telegram reassigned, or accidental
    # cross-use during testing). Bob wins.
    await repo.upsert_telegram_link(
        tenant_id=bob, telegram_chat_id=999, telegram_username="bob",
    )
    assert await repo.get_tenant_id_for_telegram_chat(999) == bob

    # Alice's row is gone — only one row exists for chat 999.
    async with repo._session_factory() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(models.TenantTelegramLink).where(
                models.TenantTelegramLink.telegram_chat_id == 999
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].tenant_id == bob


@pytest.mark.asyncio
async def test_get_returns_none_for_unlinked_chat(repo_with_telegram_table):
    repo = repo_with_telegram_table
    assert await repo.get_tenant_id_for_telegram_chat(12345) is None
