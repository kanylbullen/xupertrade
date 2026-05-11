"""Smoke test for the multi-tenancy Phase 6c alembic migration.

Same pattern as 0010's smoke test — verify the migration's structure
without spinning up Postgres. End-to-end RLS + backfill testing lives
in dashboard's testcontainers integration tests (Phase 5c).

Verifies:
- Module imports + revision metadata is chained
- Same 9 tables as 0009/0010 (no drift)
- upgrade() guards on operator-tenant existence
- upgrade() emits UPDATE WHERE tenant_id IS NULL on every table
- upgrade() flips NOT NULL on every table
- downgrade() lifts NOT NULL but does NOT undo backfill
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import call, patch


OPERATOR_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_TENANT_TABLES = {
    "trades",
    "positions",
    "equity_snapshots",
    "funding_payments",
    "backtest_runs",
    "strategy_configs",
    "manual_onchain_levels",
    "hodl_purchases",
    "user_vault_entries",
}


def _load_migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "0011_backfill_tenant_id_not_null.py"
    )
    spec = importlib.util.spec_from_file_location("mt_migration_0011", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_metadata_is_chained():
    m = _load_migration()
    assert m.revision == "0011"
    assert m.down_revision == "0010"
    assert m.branch_labels is None
    assert m.depends_on is None


def test_operator_tenant_id_constant_matches_phase_6b():
    """Phase 6b inserted operator with this exact UUID. If 6b changes,
    update this constant in lockstep — the migration FK target depends
    on it."""
    m = _load_migration()
    assert m.OPERATOR_TENANT_ID == OPERATOR_TENANT_ID


def test_tenant_tables_list_matches_0009_and_0010():
    """No drift across the three multi-tenancy migrations."""
    m = _load_migration()
    assert set(m._TABLES) == _TENANT_TABLES


def test_upgrade_guards_on_operator_existence():
    """If operator row is missing (e.g. Phase 6b never ran), backfill
    would point at a non-existent FK and fail confusingly. The guard
    raises a clear error instead."""
    m = _load_migration()
    captured_sql: list[str] = []
    with patch("alembic.op.execute") as mock_exec, \
         patch("alembic.op.alter_column") as _:
        mock_exec.side_effect = lambda sql: captured_sql.append(str(sql))
        m.upgrade()

    guard = next(s for s in captured_sql if "RAISE EXCEPTION" in s)
    assert OPERATOR_TENANT_ID in guard
    assert "operator tenant" in guard.lower()


def test_upgrade_backfills_every_table_then_alters_not_null():
    m = _load_migration()
    captured_sql: list[str] = []
    alter_calls: list[call] = []
    with patch("alembic.op.execute") as mock_exec, \
         patch("alembic.op.alter_column") as mock_alter:
        mock_exec.side_effect = lambda sql: captured_sql.append(str(sql))
        mock_alter.side_effect = lambda *a, **kw: alter_calls.append(call(*a, **kw))
        m.upgrade()

    # Every table gets a UPDATE ... WHERE tenant_id IS NULL
    for table in m._TABLES:
        update = next(
            (s for s in captured_sql
             if s.startswith(f"UPDATE {table}") and "tenant_id IS NULL" in s),
            None,
        )
        assert update is not None, f"missing backfill for {table}"
        assert OPERATOR_TENANT_ID in update

    # Every table gets ALTER COLUMN tenant_id SET NOT NULL
    for table in m._TABLES:
        assert any(
            c == call(table, "tenant_id", nullable=False)
            for c in alter_calls
        ), f"missing NOT NULL flip for {table}"


def test_downgrade_lifts_not_null_but_does_not_undo_backfill():
    """Downgrade should make NULLs allowed again, but historical rows
    keep their operator tag — re-upgrade is then a no-op for those
    rows, no double-tagging risk."""
    m = _load_migration()
    captured_sql: list[str] = []
    alter_calls: list[call] = []
    with patch("alembic.op.execute") as mock_exec, \
         patch("alembic.op.alter_column") as mock_alter:
        mock_exec.side_effect = lambda sql: captured_sql.append(str(sql))
        mock_alter.side_effect = lambda *a, **kw: alter_calls.append(call(*a, **kw))
        m.downgrade()

    # Downgrade flips nullable=True on every table
    for table in m._TABLES:
        assert any(
            c == call(table, "tenant_id", nullable=True)
            for c in alter_calls
        ), f"downgrade missing nullable=True flip for {table}"

    # Downgrade should NOT issue an UPDATE that nullifies tenant_id
    for sql in captured_sql:
        assert "SET tenant_id = NULL" not in sql, (
            "downgrade must not unbackfill — historical tag stays"
        )
