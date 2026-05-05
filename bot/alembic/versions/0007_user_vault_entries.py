"""add user_vault_entries

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_vault_entries",
        sa.Column("user_address", sa.String(length=42), nullable=False),
        sa.Column("vault_address", sa.String(length=42), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_equity_usd", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_equity_usd", sa.Float(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["vault_address"], ["vaults.address"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_address", "vault_address"),
    )


def downgrade() -> None:
    op.drop_table("user_vault_entries")
