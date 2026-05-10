"""Verify that `Repository.init_db()` does NOT create the multi-tenancy
tables (PR #37 — fix for the Phase 1 deploy race).

Background: when bot-testnet was rebuilt with Phase 1 code (2026-05-10),
its `init_db()` called `Base.metadata.create_all()` which raced ahead
of `alembic upgrade head` and created the tenant tables itself.
Alembic then crashed with "relation tenants already exists", leaving
the DB in a half-state (new tables exist, but `tenant_id` columns on
existing tables — which `create_all` doesn't add — were missing).

The fix: `init_db()` filters out the four MT tables; they're alembic's
responsibility. This test pins that contract.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from hypertrade.db.repo import _ALEMBIC_OWNED_TABLES, Repository


@pytest.fixture
async def init_db_repo():
    """Fresh in-memory SQLite repo with init_db already run. Disposes
    the engine in teardown so a failing assertion can't leak connections
    (PR #37 review)."""
    repo = Repository("sqlite+aiosqlite:///:memory:")
    await repo.init_db()
    try:
        yield repo
    finally:
        await repo._engine.dispose()


def _table_names_sync(sync_conn) -> list[str]:
    return inspect(sync_conn).get_table_names()


@pytest.mark.asyncio
async def test_init_db_skips_multi_tenancy_tables(init_db_repo):
    """init_db must NOT create tenants, tenant_bots, tenant_secrets,
    tenant_audit_log. Those are alembic's jurisdiction."""
    async with init_db_repo._engine.begin() as conn:
        tables = await conn.run_sync(_table_names_sync)
    for skipped in _ALEMBIC_OWNED_TABLES:
        assert skipped not in tables, (
            f"{skipped} must be created by alembic, not init_db"
        )


@pytest.mark.asyncio
async def test_init_db_creates_legacy_tables(init_db_repo):
    """The legacy bot tables (trades, positions, etc.) must still be
    created by init_db — that's the whole point of the function for
    fresh-deploy bootstrap before alembic is integrated."""
    async with init_db_repo._engine.begin() as conn:
        tables = await conn.run_sync(_table_names_sync)
    for required in (
        "trades", "positions", "equity_snapshots",
        "funding_payments", "backtest_runs",
    ):
        assert required in tables, f"{required} must be created by init_db"


@pytest.mark.asyncio
async def test_alembic_owned_set_matches_actual_mt_tables():
    """Sanity: the constant lists the four Phase 1 MT tables exactly.
    If a future phase adds another MT table, this assertion fires and
    the developer adds it to `_ALEMBIC_OWNED_TABLES` too."""
    expected = {
        "tenants", "tenant_bots", "tenant_secrets", "tenant_audit_log",
    }
    assert set(_ALEMBIC_OWNED_TABLES) == expected
