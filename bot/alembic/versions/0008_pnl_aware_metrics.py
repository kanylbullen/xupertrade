"""pnl-aware metrics: vault_nav_history.pnl_cum + user_vault_entries refactor

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-05

Replaces the early-iteration `first_seen_*` / `last_seen_equity_usd`
columns on `user_vault_entries` with the richer `vault_equity_usd`,
`unrealized_pnl_usd`, `all_time_pnl_usd`, `entered_at`, `days_following`
fields driven directly by HL's `vaultDetails.followerState`. Also adds
`pnl_cum` to `vault_nav_history` so period returns can be computed
flow-neutrally.

Existing user_vault_entries rows are dropped — only ~1 day of data and
they were tracking the wrong field anyway.
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Wipe early-iteration rows; the new schema is incompatible and the
    # data was misinterpreting HL's `equity` field anyway.
    op.execute("DELETE FROM user_vault_entries")

    op.add_column(
        "user_vault_entries",
        sa.Column("vault_equity_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "user_vault_entries",
        sa.Column(
            "unrealized_pnl_usd", sa.Float(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "user_vault_entries",
        sa.Column(
            "all_time_pnl_usd", sa.Float(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "user_vault_entries",
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_vault_entries",
        sa.Column(
            "days_following", sa.Integer(), nullable=False, server_default="0"
        ),
    )

    op.drop_column("user_vault_entries", "first_seen_at")
    op.drop_column("user_vault_entries", "first_seen_equity_usd")
    op.drop_column("user_vault_entries", "last_seen_equity_usd")

    # pnl_cum is nullable — NULL means "we don't have it for this point"
    # (legacy rows or missing HL data), distinct from a real PnL of zero.
    # Existing rows backfill to NULL; new rows get the value when known.
    op.add_column(
        "vault_nav_history",
        sa.Column("pnl_cum", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vault_nav_history", "pnl_cum")

    op.add_column(
        "user_vault_entries",
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "user_vault_entries",
        sa.Column(
            "first_seen_equity_usd", sa.Float(), nullable=True
        ),
    )
    op.add_column(
        "user_vault_entries",
        sa.Column(
            "last_seen_equity_usd", sa.Float(), nullable=True
        ),
    )

    op.drop_column("user_vault_entries", "days_following")
    op.drop_column("user_vault_entries", "entered_at")
    op.drop_column("user_vault_entries", "all_time_pnl_usd")
    op.drop_column("user_vault_entries", "unrealized_pnl_usd")
    op.drop_column("user_vault_entries", "vault_equity_usd")
