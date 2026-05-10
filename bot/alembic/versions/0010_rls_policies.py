"""multi-tenancy Phase 5a: RLS policies + app_tenant_id() helper

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-10

Enables row-level security on the 9 per-tenant data tables and adds
the `tenant_isolation` policy that filters every row by
`tenant_id = app_tenant_id()`. The helper function decodes the
current Postgres role's name (`tenant_<32hex>` form) into the
tenant's UUID.

**No new roles created here** — Phase 5b's bot orchestrator does that
at bot-create time. This migration is pure infrastructure: when a
tenant role exists and connects, it's filtered; until then, only the
operator's `postgres` superuser connects and bypasses RLS naturally
(no FORCE ROW LEVEL SECURITY → superuser unaffected).

**Operator's current 3-mode deploy is unaffected** because the bot
containers connect as the postgres superuser. Their queries continue
to see all rows including tenant_id=NULL legacy data.

Reversible via downgrade (drops policies + function + DISABLE RLS).
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


_TENANT_TABLES = (
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


def upgrade() -> None:
    # Helper function: extract tenant_id from current_user role name.
    # Role name contract (Phase 5b orchestrator must follow):
    #   tenant_<32hex>  — 32 hex chars from the tenant UUID, dashes
    #   stripped, lowercase
    # Function reconstructs the canonical 8-4-4-4-12 UUID form and
    # casts to UUID. Returns NULL for any role that doesn't match
    # the pattern (operator's postgres role, etc.).
    #
    # Marked STABLE (NOT IMMUTABLE — PR #45 review fix). `current_user`
    # is session-scoped and can change via `SET ROLE`, so the function
    # is not truly constant. IMMUTABLE would let the planner
    # constant-fold the call across roles using cached plans, which
    # would BREAK tenant isolation. STABLE is the right marking:
    # value is constant within a transaction but can vary across them.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS UUID AS $$
        BEGIN
            IF current_user NOT LIKE 'tenant\\_%' ESCAPE '\\' THEN
                RETURN NULL;
            END IF;
            -- Role: tenant_3a2f1e4caaaabbbbccccdddd11112222 (39 chars)
            -- Reconstruct canonical UUID:  3a2f1e4c-aaaa-bbbb-cccc-dddd11112222
            RETURN (
                substring(current_user from 8 for 8) || '-' ||
                substring(current_user from 16 for 4) || '-' ||
                substring(current_user from 20 for 4) || '-' ||
                substring(current_user from 24 for 4) || '-' ||
                substring(current_user from 28 for 12)
            )::uuid;
        EXCEPTION WHEN OTHERS THEN
            -- Malformed role name (non-hex, wrong length) → NULL.
            -- Caller is RLS policy; NULL never matches a real
            -- tenant_id, so the role sees nothing.
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql STABLE;
        """
    )

    # Per-tenant table: enable RLS + create the tenant_isolation policy.
    # USING gates SELECT/UPDATE/DELETE; WITH CHECK gates INSERT/UPDATE
    # so a tenant role can never write a row tagged with another
    # tenant's id (or NULL — that would fall to the operator).
    #
    # Superuser (postgres) bypasses RLS by default — no FORCE here.
    # Operator's current bots keep working unchanged.
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              FOR ALL
              USING (tenant_id = app_tenant_id())
              WITH CHECK (tenant_id = app_tenant_id());
            """
        )


def downgrade() -> None:
    # Drop policies + disable RLS in reverse — tables become open
    # again. The function survives slightly longer because dropping
    # it before any policy still references it would error; we drop
    # it last.
    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
    op.execute("DROP FUNCTION IF EXISTS app_tenant_id();")
