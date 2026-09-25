"""alembic 0017: funding_payments keyed on (tenant_id, mode, coin, timestamp).

UNIQUE(hash) kept the table at one row, because HyperLiquid sends the
same all-zero hash for every funding event (roadmap NU-6.1).

The smoke tests always run. The Postgres tests need
MIGRATION_TEST_DATABASE_URL, a server where that user may CREATE
DATABASE: CI's `migrations` job sets it, and each test migrates a
scratch database of its own and drops it afterwards. Locally:

    docker run --rm -d --name mig -p 127.0.0.1:55432:5432 \\
        -e POSTGRES_PASSWORD=postgres postgres:16-alpine
    MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \\
        uv run pytest -q tests/test_db/test_migration_0017_funding_key.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint

from hypertrade.db.models import FundingPayment

BOT_DIR = Path(__file__).resolve().parents[2]
PG_URL = os.environ.get("MIGRATION_TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(
    not PG_URL, reason="MIGRATION_TEST_DATABASE_URL is not set (needs Postgres)",
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
ZERO_HASH = "0x" + "0" * 64
T0 = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)


def _load_migration():
    path = BOT_DIR / "alembic" / "versions" / "0017_funding_payments_event_key.py"
    spec = importlib.util.spec_from_file_location("migration_0017", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_is_chained_after_0016():
    m = _load_migration()
    assert (m.revision, m.down_revision) == ("0017", "0016")
    assert m.branch_labels is None and m.depends_on is None


def test_the_model_declares_the_key_the_migration_creates():
    """init_db's create_all and alembic must agree, or a fresh install
    and a migrated one dedupe differently."""
    m = _load_migration()
    table = FundingPayment.__table__
    uniques = [
        (c.name, [col.name for col in c.columns])
        for c in table.constraints if isinstance(c, UniqueConstraint)
    ]
    assert uniques == [(m.EVENT_KEY, m.EVENT_KEY_COLUMNS)]
    assert not table.c.hash.unique


# ── Postgres ─────────────────────────────────────────────────────────


def _plain(url: str) -> str:
    """asyncpg's own connect() takes no SQLAlchemy driver suffix."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    """A real `alembic` process, as the operator runs it — and not
    in-process, where env.py's asyncio.run would meet the test's loop
    (CLAUDE.md § 9)."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BOT_DIR,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _migrate(url: str, target: str) -> None:
    proc = _alembic(url, "upgrade", target)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture
async def scratch_db():
    import asyncpg

    name = f"mig0017_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_plain(PG_URL))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    yield PG_URL.rsplit("/", 1)[0] + "/" + name
    admin = await asyncpg.connect(_plain(PG_URL))
    try:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(_plain(url))


async def _unique_keys(conn) -> set[tuple[str, tuple[str, ...]]]:
    """Every unique index on funding_payments but the primary key —
    constraints and bare indexes alike — with its columns."""
    rows = await conn.fetch(
        """
        SELECT ic.relname AS name,
               array_agg(a.attname::text ORDER BY k.ord) AS cols
        FROM pg_index x
        JOIN pg_class ic ON ic.oid = x.indexrelid
        CROSS JOIN LATERAL unnest(x.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum
        WHERE x.indrelid = 'funding_payments'::regclass
          AND x.indisunique AND NOT x.indisprimary
        GROUP BY ic.relname
        """
    )
    return {(r["name"], tuple(r["cols"])) for r in rows}


async def _add_tenant(conn) -> None:
    await conn.execute(
        "INSERT INTO tenants (id, authentik_sub, email) VALUES ($1, 'op', 'op@example.com')",
        TENANT,
    )


async def _add_funding(
    conn, ts: datetime, *, coin: str = "BTC", hash_: str = ZERO_HASH,
) -> None:
    await conn.execute(
        """
        INSERT INTO funding_payments
            (tenant_id, "timestamp", hash, coin, usdc, mode, is_paper)
        VALUES ($1, $2, $3, $4, -0.5, 'testnet', false)
        """,
        TENANT, ts, hash_, coin,
    )


EVENT_KEY = ("uq_funding_payments_event", ("tenant_id", "mode", "coin", "timestamp"))
OLD_KEY = ("funding_payments_hash_key", ("hash",))


@needs_pg
async def test_upgrade_head_on_an_empty_database(scratch_db):
    _migrate(scratch_db, "head")
    conn = await _connect(scratch_db)
    try:
        assert await _unique_keys(conn) == {EVENT_KEY}
    finally:
        await conn.close()


@needs_pg
async def test_upgrade_keeps_the_old_row_and_stores_the_next_zero_hash(scratch_db):
    import asyncpg

    _migrate(scratch_db, "0016")
    conn = await _connect(scratch_db)
    try:
        assert await _unique_keys(conn) == {OLD_KEY}
        await _add_tenant(conn)
        await _add_funding(conn, T0)
        # The bug: the next hour's funding shares the all-zero hash.
        with pytest.raises(asyncpg.UniqueViolationError):
            await _add_funding(conn, T0 + timedelta(hours=1))
    finally:
        await conn.close()

    _migrate(scratch_db, "head")

    conn = await _connect(scratch_db)
    try:
        assert await _unique_keys(conn) == {EVENT_KEY}
        old = await conn.fetch('SELECT "timestamp", hash FROM funding_payments')
        assert [(r["timestamp"], r["hash"]) for r in old] == [(T0, ZERO_HASH)]
        await _add_funding(conn, T0 + timedelta(hours=1))
        await _add_funding(conn, T0, coin="ETH")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _add_funding(conn, T0)
        assert await conn.fetchval("SELECT count(*) FROM funding_payments") == 3
    finally:
        await conn.close()


@needs_pg
async def test_upgrade_fails_rather_than_deletes_a_duplicate_event(scratch_db):
    _migrate(scratch_db, "0016")
    conn = await _connect(scratch_db)
    try:
        await _add_tenant(conn)
        # Only possible with distinct hashes; the old key allowed it.
        await _add_funding(conn, T0, hash_="0xa")
        await _add_funding(conn, T0, hash_="0xb")
    finally:
        await conn.close()

    proc = _alembic(scratch_db, "upgrade", "head")
    assert proc.returncode != 0
    assert "could not create unique index" in proc.stderr

    conn = await _connect(scratch_db)
    try:
        assert await _unique_keys(conn) == {OLD_KEY}  # rolled back whole
        assert await conn.fetchval("SELECT count(*) FROM funding_payments") == 2
    finally:
        await conn.close()


@needs_pg
async def test_the_repository_dedupes_with_on_conflict_on_postgres(scratch_db):
    from hypertrade.db.repo import Repository
    from hypertrade.engine import funding

    _migrate(scratch_db, "head")
    conn = await _connect(scratch_db)
    try:
        await _add_tenant(conn)
    finally:
        await conn.close()

    t0_ms = funding.to_ms(T0)
    page = [
        {"time": t0_ms + i * 3_600_000, "hash": ZERO_HASH,
         "delta": {"type": "funding", "coin": "BTC", "usdc": "-0.5", "szi": "0.01"}}
        for i in range(2)
    ]

    async def fetch(start_ms, end_ms):
        return page

    repo = Repository(scratch_db, tenant_id=str(TENANT), mode="testnet")
    try:
        first = await funding.ingest_funding(repo, fetch, t0_ms)
        again = await funding.ingest_funding(repo, fetch, t0_ms)
        dry = await funding.ingest_funding(repo, fetch, t0_ms, apply=False)
        latest = await repo.get_latest_funding_timestamp()
    finally:
        await repo.close()

    assert (first.new, again.new, dry.new) == (2, 0, 0)
    assert latest == T0 + timedelta(hours=1)


@needs_pg
async def test_downgrade_refuses_while_rows_share_a_hash(scratch_db):
    _migrate(scratch_db, "head")
    conn = await _connect(scratch_db)
    try:
        await _add_tenant(conn)
        await _add_funding(conn, T0)
        await _add_funding(conn, T0 + timedelta(hours=1))
    finally:
        await conn.close()

    proc = _alembic(scratch_db, "downgrade", "0016")
    assert proc.returncode != 0
    assert "could not create unique index" in proc.stderr

    conn = await _connect(scratch_db)
    try:
        assert await _unique_keys(conn) == {EVENT_KEY}  # rolled back whole
        await conn.execute('DELETE FROM funding_payments WHERE "timestamp" > $1', T0)
    finally:
        await conn.close()

    proc = _alembic(scratch_db, "downgrade", "0016")
    assert proc.returncode == 0, proc.stderr
    conn = await _connect(scratch_db)
    try:
        assert await _unique_keys(conn) == {OLD_KEY}
    finally:
        await conn.close()
