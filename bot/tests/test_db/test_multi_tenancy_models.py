"""Tests for the multi-tenancy schema (Phase 1, PR #35 plan).

Verifies that the new tables (`tenants`, `tenant_bots`,
`tenant_secrets`, `tenant_audit_log`) instantiate correctly, that FK
relationships work, and that cascade-delete on tenant wipes everything
that depends on it.

These tests run on SQLite in-memory; the production migration uses
Postgres-specific syntax (`gen_random_uuid()` server default, BYTEA),
but the SQLAlchemy models use the cross-DB `Uuid` type so the same
classes work for both.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from hypertrade.db import models
from hypertrade.db.models import (
    Base,
    Tenant,
    TenantAuditLog,
    TenantBot,
    TenantSecret,
)


@pytest.fixture
async def session():
    """Fresh in-memory SQLite per test, with FK enforcement enabled
    (required for ON DELETE CASCADE — SQLite default is OFF)."""
    from sqlalchemy import event

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fks(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _make_tenant(
    session, *, sub: str = "test-sub", multi_bot: bool = False, operator: bool = False
) -> Tenant:
    t = Tenant(
        authentik_sub=sub,
        email=f"{sub}@example.com",
        display_name=sub,
        multi_bot_enabled=multi_bot,
        is_operator=operator,
    )
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@pytest.mark.asyncio
async def test_tenant_minimal_insert(session):
    """A tenant inserts with only Authentik sub + email; passphrase is
    set later via the onboarding flow."""
    t = await _make_tenant(session, sub="user-1")
    assert t.id is not None
    assert isinstance(t.id, uuid.UUID)
    assert t.is_active is True
    assert t.is_operator is False
    assert t.multi_bot_enabled is False
    assert t.passphrase_salt is None
    assert t.passphrase_verifier is None


@pytest.mark.asyncio
async def test_tenant_unique_authentik_sub(session):
    """Two tenants cannot share the same Authentik sub."""
    await _make_tenant(session, sub="dup-sub")
    with pytest.raises(IntegrityError):
        await _make_tenant(session, sub="dup-sub")


@pytest.mark.asyncio
async def test_tenant_bot_unique_per_mode(session):
    """A tenant can have at most one bot per mode (UNIQUE)."""
    t = await _make_tenant(session, sub="user-2")
    session.add(TenantBot(tenant_id=t.id, mode="paper"))
    await session.commit()

    session.add(TenantBot(tenant_id=t.id, mode="paper"))  # duplicate mode
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.asyncio
async def test_tenant_bot_different_modes_allowed(session):
    """Same tenant + different mode = OK (no UNIQUE collision)."""
    t = await _make_tenant(session, sub="user-2b")
    session.add(TenantBot(tenant_id=t.id, mode="paper"))
    session.add(TenantBot(tenant_id=t.id, mode="testnet"))
    await session.commit()
    bots = (await session.scalars(
        select(TenantBot).where(TenantBot.tenant_id == t.id)
    )).all()
    assert {b.mode for b in bots} == {"paper", "testnet"}


@pytest.mark.asyncio
async def test_tenant_bot_three_modes_for_multi_bot_tenant(session):
    """The operator gets all three modes."""
    t = await _make_tenant(session, sub="op", multi_bot=True, operator=True)
    for mode in ("paper", "testnet", "mainnet"):
        session.add(TenantBot(tenant_id=t.id, mode=mode))
    await session.commit()

    bots = (await session.scalars(
        select(TenantBot).where(TenantBot.tenant_id == t.id)
    )).all()
    assert {b.mode for b in bots} == {"paper", "testnet", "mainnet"}


@pytest.mark.asyncio
async def test_tenant_secret_composite_pk(session):
    """tenant_secrets PK is (tenant_id, key) — same key for two tenants
    is fine, same key twice for one tenant is not."""
    t1 = await _make_tenant(session, sub="user-3a")
    t2 = await _make_tenant(session, sub="user-3b")

    session.add(TenantSecret(
        tenant_id=t1.id, key="HYPERLIQUID_PRIVATE_KEY",
        ciphertext=b"\x01\x02", nonce=b"\x00" * 12,
    ))
    session.add(TenantSecret(
        tenant_id=t2.id, key="HYPERLIQUID_PRIVATE_KEY",
        ciphertext=b"\x03\x04", nonce=b"\x00" * 12,
    ))
    await session.commit()

    session.add(TenantSecret(
        tenant_id=t1.id, key="HYPERLIQUID_PRIVATE_KEY",
        ciphertext=b"\x05", nonce=b"\x00" * 12,
    ))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_cascade_delete_tenant_wipes_everything(session):
    """`DELETE FROM tenants WHERE id=?` must cascade to tenant_bots,
    tenant_secrets, tenant_audit_log + every existing-table row that
    references the tenant. Critical for GDPR (PR #35 §11.8)."""
    t = await _make_tenant(session, sub="user-4")

    session.add(TenantBot(tenant_id=t.id, mode="paper"))
    session.add(TenantSecret(
        tenant_id=t.id, key="API_KEY",
        ciphertext=b"\x01", nonce=b"\x00" * 12,
    ))
    session.add(TenantAuditLog(
        tenant_id=t.id, actor="tenant", action="secret.set",
    ))
    # Link a Trade and a PositionRecord too
    session.add(models.Trade(
        tenant_id=t.id, order_id="cascade-test-1",
        strategy_name="bb_short", symbol="SOL", side="sell",
        size=1.0, price=100.0,
    ))
    session.add(models.PositionRecord(
        tenant_id=t.id, strategy_name="bb_short", symbol="SOL",
        side="short", size=1.0, entry_price=100.0,
    ))
    await session.commit()

    # Sanity: rows exist
    assert (await session.scalars(
        select(TenantBot).where(TenantBot.tenant_id == t.id)
    )).first() is not None
    assert (await session.scalars(
        select(models.Trade).where(models.Trade.tenant_id == t.id)
    )).first() is not None

    # Delete the tenant
    await session.delete(t)
    await session.commit()

    # Everything that referenced it must be gone
    assert (await session.scalars(
        select(TenantBot).where(TenantBot.tenant_id == t.id)
    )).first() is None
    assert (await session.scalars(
        select(TenantSecret).where(TenantSecret.tenant_id == t.id)
    )).first() is None
    assert (await session.scalars(
        select(TenantAuditLog).where(TenantAuditLog.tenant_id == t.id)
    )).first() is None
    assert (await session.scalars(
        select(models.Trade).where(models.Trade.tenant_id == t.id)
    )).first() is None
    assert (await session.scalars(
        select(models.PositionRecord).where(models.PositionRecord.tenant_id == t.id)
    )).first() is None


@pytest.mark.asyncio
async def test_existing_table_tenant_id_nullable(session):
    """Phase 1 backwards-compat: existing data tables accept NULL
    tenant_id so the operator's current bots keep writing rows
    unchanged. Phase 6 makes it NOT NULL after backfill."""
    session.add(models.Trade(
        order_id="phase1-null-tenant", strategy_name="bb_short",
        symbol="SOL", side="buy", size=1.0, price=100.0,
        # no tenant_id
    ))
    await session.commit()
    row = (await session.scalars(
        select(models.Trade).where(models.Trade.order_id == "phase1-null-tenant")
    )).one()
    assert row.tenant_id is None


@pytest.mark.asyncio
async def test_audit_log_append_only_indexed_on_tenant_ts(session):
    """Audit log writes accumulate ordered; the (tenant_id, ts) index
    supports the typical recent-activity query."""
    t = await _make_tenant(session, sub="user-5")
    for i, action in enumerate(("secret.set", "bot.start", "bot.stop")):
        session.add(TenantAuditLog(
            tenant_id=t.id, actor="tenant", action=action,
            context_json=f'{{"i": {i}}}',
        ))
    await session.commit()

    rows = (await session.scalars(
        select(TenantAuditLog)
        .where(TenantAuditLog.tenant_id == t.id)
        .order_by(TenantAuditLog.ts.asc())
    )).all()
    assert [r.action for r in rows] == ["secret.set", "bot.start", "bot.stop"]
