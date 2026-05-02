"""add funding_payments table

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "funding_payments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hash", sa.String(length=80), nullable=False),
        sa.Column("coin", sa.String(length=16), nullable=False),
        sa.Column("usdc", sa.Float(), nullable=False),
        sa.Column("szi", sa.Float(), nullable=True),
        sa.Column("funding_rate", sa.Float(), nullable=True),
        sa.Column("strategy_name", sa.String(length=64), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash"),
    )
    op.create_index("ix_funding_payments_timestamp", "funding_payments", ["timestamp"])
    op.create_index("ix_funding_payments_coin", "funding_payments", ["coin"])
    op.create_index("ix_funding_payments_strategy_name", "funding_payments", ["strategy_name"])
    op.create_index("ix_funding_payments_is_paper", "funding_payments", ["is_paper"])
    op.create_index("ix_funding_payments_mode", "funding_payments", ["mode"])


def downgrade() -> None:
    op.drop_index("ix_funding_payments_mode", "funding_payments")
    op.drop_index("ix_funding_payments_is_paper", "funding_payments")
    op.drop_index("ix_funding_payments_strategy_name", "funding_payments")
    op.drop_index("ix_funding_payments_coin", "funding_payments")
    op.drop_index("ix_funding_payments_timestamp", "funding_payments")
    op.drop_table("funding_payments")
