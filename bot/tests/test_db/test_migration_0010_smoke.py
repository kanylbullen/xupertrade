"""Smoke test for the multi-tenancy Phase 5a alembic migration.

Real RLS testing requires a running Postgres (SQLite has no RLS). That
lands in Phase 5c as an integration test. Here we just verify:

- The migration module imports without syntax errors
- Revision metadata (revision + down_revision) is correctly chained
- The expected per-tenant tables are listed
- upgrade() emits expected SQL fragments (sniff test for typos)
- downgrade() reverses the operations

Catches the "I committed a typo'd migration" class of bug without
needing a Postgres in CI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch


def _load_migration():
    """Load the migration by file path — the file name starts with a
    digit so a regular `import` statement won't work."""
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic" / "versions" / "0010_rls_policies.py"
    )
    spec = importlib.util.spec_from_file_location("mt_migration_0010", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_revision_metadata_is_chained():
    m = _load_migration()
    assert m.revision == "0010"
    assert m.down_revision == "0009"
    assert m.branch_labels is None
    assert m.depends_on is None


def test_tenant_tables_list_matches_phase_1_schema():
    """The 9 tables we add tenant_id to in alembic 0009 must all get
    RLS in 0010 — no drift."""
    m = _load_migration()
    expected = {
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
    assert set(m._TENANT_TABLES) == expected


def test_upgrade_creates_function_and_policies():
    """Capture SQL emitted by upgrade() and verify each per-tenant
    table gets ENABLE RLS + tenant_isolation policy."""
    m = _load_migration()
    captured: list[str] = []
    with patch("alembic.op.execute") as mock_exec:
        mock_exec.side_effect = lambda sql: captured.append(str(sql))
        m.upgrade()

    joined = "\n".join(captured)
    assert "CREATE OR REPLACE FUNCTION app_tenant_id()" in joined
    for table in m._TENANT_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in joined
        assert f"CREATE POLICY tenant_isolation ON {table}" in joined
        assert "tenant_id = app_tenant_id()" in joined  # USING clause
    assert "WITH CHECK" in joined  # write-side enforcement


def test_downgrade_reverses_upgrade():
    m = _load_migration()
    captured: list[str] = []
    with patch("alembic.op.execute") as mock_exec:
        mock_exec.side_effect = lambda sql: captured.append(str(sql))
        m.downgrade()

    joined = "\n".join(captured)
    for table in m._TENANT_TABLES:
        assert f"DROP POLICY IF EXISTS tenant_isolation ON {table}" in joined
        assert f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY" in joined
    assert "DROP FUNCTION IF EXISTS app_tenant_id()" in joined


def test_role_name_pattern_documented_in_function_body():
    """The orchestrator (Phase 5b) must follow the `tenant_<32hex>`
    role-name contract. The function body should reference this
    pattern explicitly so a future reader sees the expected shape."""
    m = _load_migration()
    captured: list[str] = []
    with patch("alembic.op.execute") as mock_exec:
        mock_exec.side_effect = lambda sql: captured.append(str(sql))
        m.upgrade()
    function_sql = next(s for s in captured if "app_tenant_id" in s)
    assert "tenant\\_%" in function_sql or "tenant_" in function_sql
    # Reconstruction breaks UUID into 8-4-4-4-12 form
    assert "for 8" in function_sql
    assert "for 12" in function_sql


def test_app_tenant_id_function_is_stable_not_immutable():
    """PR #45 review fix. `current_user` is session-scoped, so the
    function isn't truly constant — IMMUTABLE would let the planner
    constant-fold calls across roles using cached plans and BREAK
    tenant isolation. STABLE is the right marking."""
    m = _load_migration()
    captured: list[str] = []
    with patch("alembic.op.execute") as mock_exec:
        mock_exec.side_effect = lambda sql: captured.append(str(sql))
        m.upgrade()
    function_sql = next(s for s in captured if "app_tenant_id" in s)
    assert "STABLE" in function_sql, (
        "app_tenant_id() must be STABLE, not IMMUTABLE — "
        "current_user is session-scoped"
    )
    assert "IMMUTABLE" not in function_sql
