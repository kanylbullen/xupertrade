"""multi-tenancy schema: tenants, tenant_bots, tenant_secrets, tenant_audit_log
+ tenant_id columns on existing data tables (NULLABLE — backwards compat)

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-10

Phase 1 of the multi-tenancy rollout (see docs/plans/multi-tenancy.md).
Pure additive migration: new tables + new nullable columns. No data
migration, no constraint tightening — those happen in Phase 6 cutover.

The application keeps working unchanged with NULL tenant_id rows; the
operator's existing 3 bots produce NULL-tenant rows until Phase 6
backfills them.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


_TABLES_NEEDING_TENANT_ID = (
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
    # ---- new core multi-tenancy tables ----
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        # authentik_sub is unique — enforced by the named index below
        # (skipping column-level `unique=True` to avoid a redundant
        # auto-generated unique constraint).
        sa.Column("authentik_sub", sa.String(128), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("passphrase_salt", sa.LargeBinary(16), nullable=True),
        sa.Column("passphrase_verifier", sa.LargeBinary(32), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "is_operator",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "multi_bot_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_tenants_authentik_sub", "tenants", ["authentik_sub"], unique=True
    )

    op.create_table(
        "tenant_bots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=True),
        sa.Column("container_name", sa.String(128), nullable=True),
        sa.Column(
            "is_running",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("telegram_webhook_secret", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "mode", name="uq_tenant_bots_mode"),
    )
    op.create_index("idx_tenant_bots_tenant", "tenant_bots", ["tenant_id"])
    op.create_index(
        "idx_tenant_bots_running",
        "tenant_bots",
        ["is_running"],
        postgresql_where=sa.text("is_running = true"),
    )

    op.create_table(
        "tenant_secrets",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(12), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "tenant_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("context_json", sa.Text(), server_default=sa.text("'{}'")),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_tenant_audit_log_tenant_ts",
        "tenant_audit_log",
        ["tenant_id", "ts"],
    )

    # ---- tenant_id (nullable FK) on existing data tables ----
    for table in _TABLES_NEEDING_TENANT_ID:
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            f"idx_{table}_tenant", table, ["tenant_id"]
        )


def downgrade() -> None:
    # Drop in reverse order: indexes + columns on existing tables first,
    # then the new tables (which the FK references depend on).
    for table in _TABLES_NEEDING_TENANT_ID:
        op.drop_index(f"idx_{table}_tenant", table_name=table)
        op.drop_column(table, "tenant_id")

    op.drop_index("idx_tenant_audit_log_tenant_ts", table_name="tenant_audit_log")
    op.drop_table("tenant_audit_log")

    op.drop_table("tenant_secrets")

    op.drop_index("idx_tenant_bots_running", table_name="tenant_bots")
    op.drop_index("idx_tenant_bots_tenant", table_name="tenant_bots")
    op.drop_table("tenant_bots")

    op.drop_index("idx_tenants_authentik_sub", table_name="tenants")
    op.drop_table("tenants")
