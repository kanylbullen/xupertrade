"""multi-tenancy Phase 6c: backfill tenant_id on existing rows + flip NOT NULL

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-11

Closes the gap left by 0009 (which added `tenant_id` as NULLABLE on the
9 per-tenant data tables for backwards compat). Phase 6c wires
dashboard data routes to filter by tenant via per-tenant Postgres roles
+ RLS, which means rows without a `tenant_id` become invisible to
everyone.

This migration:
  1. Backfills `tenant_id` to operator's UUID on every NULL row (all
     pre-multi-tenancy data belongs to operator since they were the
     only user before today).
  2. Flips `tenant_id` to NOT NULL so future inserts can never be
     tenant-less.

The operator row is required only when step 1 has rows to backfill. A
database with no NULL-tenant rows (a fresh, empty one) migrates without
it, which is what lets CI run `alembic upgrade head` on an empty DB.

Run on production AFTER taking a ZFS snapshot — the NULL → operator
mapping is irreversible without one. Downgrade lifts the NOT NULL
constraint but does NOT undo the backfill (data stays tagged).
"""

from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


# Operator's tenant UUID, set by the Phase 6b SQL backfill (run on the
# host once during the multi-tenancy cutover). Hardcoded here because
# alembic migrations can't import application code; if 6b's UUID ever
# changes, this constant + the matching default in docker-compose.yml's
# x-bot-env block (TENANT_ID) must both be updated in lockstep.
OPERATOR_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# Same list as 0009 — keep in sync. Order doesn't matter for the
# UPDATE/ALTER but matches 0009 for readability.
_TABLES = (
    "trades",
    "positions",
    "equity_snapshots",
    "funding_payments",
    "backtest_runs",
    "strategy_configs",
    "manual_onchain_levels",
    "hodl_purchases",
    "user_vault_entries",
)


def _null_tenant_rows_exist_sql() -> str:
    """SQL boolean: does any row in `_TABLES` still lack a tenant?"""
    return " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} WHERE tenant_id IS NULL)"
        for table in _TABLES
    )


def upgrade() -> None:
    # Defensive: bail with a clear error if the backfill below would
    # need the operator row and it isn't there. Without it the UPDATE
    # points at a non-existent FK and fails confusingly.
    #
    # The row is only needed when there is something to backfill. On a
    # database with no NULL-tenant rows — a fresh, empty one (CI, a new
    # host) — every UPDATE below touches zero rows, so no FK is ever
    # checked, and SET NOT NULL succeeds on an empty column. Requiring
    # the row there made `alembic upgrade head` impossible on an empty
    # database. Where the operator row exists (production), the first
    # condition is false exactly as before, and the UPDATEs and ALTERs
    # run unchanged.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM tenants WHERE id = '{OPERATOR_TENANT_ID}'
            ) AND ({_null_tenant_rows_exist_sql()}) THEN
                RAISE EXCEPTION 'Phase 6b operator tenant {OPERATOR_TENANT_ID} not found — run 6b backfill first';
            END IF;
        END $$;
        """
    )

    for table in _TABLES:
        op.execute(
            f"UPDATE {table} SET tenant_id = '{OPERATOR_TENANT_ID}' "
            f"WHERE tenant_id IS NULL"
        )
        op.alter_column(table, "tenant_id", nullable=False)


def downgrade() -> None:
    # Lift the NOT NULL so old code paths that insert tenant-less rows
    # don't fail. Don't reverse the backfill — historical rows stay
    # tagged to operator, which is harmless and prevents loss of the
    # tag on re-upgrade.
    for table in _TABLES:
        op.alter_column(table, "tenant_id", nullable=True)
