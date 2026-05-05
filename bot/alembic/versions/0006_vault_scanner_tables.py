"""add vaults, vault_snapshots, vault_nav_history

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vaults",
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("leader_address", sa.String(length=42), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profit_share_pct", sa.Float(), nullable=True),
        sa.Column("relationship_type", sa.String(length=16), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("address"),
    )
    op.create_index("ix_vaults_leader_address", "vaults", ["leader_address"])

    op.create_table(
        "vault_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("vault_address", sa.String(length=42), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aum_usd", sa.Float(), nullable=True),
        sa.Column("nav", sa.Float(), nullable=True),
        sa.Column("leader_equity_pct", sa.Float(), nullable=True),
        sa.Column("depositor_count", sa.Integer(), nullable=True),
        sa.Column("apr", sa.Float(), nullable=True),
        sa.Column("age_days", sa.Integer(), nullable=True),
        sa.Column("roi_7d", sa.Float(), nullable=True),
        sa.Column("roi_30d", sa.Float(), nullable=True),
        sa.Column("roi_90d", sa.Float(), nullable=True),
        sa.Column("roi_180d", sa.Float(), nullable=True),
        sa.Column("roi_365d", sa.Float(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("sharpe_180d", sa.Float(), nullable=True),
        sa.Column("qualified", sa.Boolean(), nullable=True),
        sa.Column("filter_breakdown_json", sa.Text(), nullable=True),
        sa.Column("allow_deposits", sa.Boolean(), nullable=True),
        sa.Column("is_closed", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["vault_address"], ["vaults.address"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "vault_address", "snapshot_at", name="uq_vault_snapshot"
        ),
    )
    op.create_index(
        "ix_vault_snapshots_vault_address",
        "vault_snapshots",
        ["vault_address"],
    )
    op.create_index(
        "ix_vault_snapshots_snapshot_at",
        "vault_snapshots",
        ["snapshot_at"],
    )
    op.create_index(
        "ix_vault_snapshots_qualified",
        "vault_snapshots",
        ["qualified"],
    )

    op.create_table(
        "vault_nav_history",
        sa.Column("vault_address", sa.String(length=42), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nav", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["vault_address"], ["vaults.address"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("vault_address", "timestamp"),
    )


def downgrade() -> None:
    op.drop_table("vault_nav_history")
    op.drop_index("ix_vault_snapshots_qualified", "vault_snapshots")
    op.drop_index("ix_vault_snapshots_snapshot_at", "vault_snapshots")
    op.drop_index("ix_vault_snapshots_vault_address", "vault_snapshots")
    op.drop_table("vault_snapshots")
    op.drop_index("ix_vaults_leader_address", "vaults")
    op.drop_table("vaults")
