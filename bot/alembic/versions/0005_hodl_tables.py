"""add manual_onchain_levels and hodl_purchases

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-04
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_onchain_levels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sth_cost_basis_usd", sa.Float(), nullable=True),
        sa.Column("lth_cost_basis_usd", sa.Float(), nullable=True),
        sa.Column("realized_price_usd", sa.Float(), nullable=True),
        sa.Column("cvdd_usd", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_onchain_levels_recorded_at",
                    "manual_onchain_levels", ["recorded_at"])

    op.create_table(
        "hodl_purchases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=True),
        sa.Column("amount_local", sa.Float(), nullable=False),
        sa.Column("local_currency", sa.String(length=8), nullable=True),
        sa.Column("btc_amount", sa.Float(), nullable=False),
        sa.Column("btc_price_usd", sa.Float(), nullable=False),
        sa.Column("btc_price_local", sa.Float(), nullable=True),
        sa.Column("fx_rate", sa.Float(), nullable=True),
        sa.Column("zone", sa.String(length=16), nullable=True),
        sa.Column("cold_storage_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cold_storage_address", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hodl_purchases_purchased_at",
                    "hodl_purchases", ["purchased_at"])
    op.create_index("ix_hodl_purchases_asset",
                    "hodl_purchases", ["asset"])


def downgrade() -> None:
    op.drop_index("ix_hodl_purchases_asset", "hodl_purchases")
    op.drop_index("ix_hodl_purchases_purchased_at", "hodl_purchases")
    op.drop_table("hodl_purchases")
    op.drop_index("ix_manual_onchain_levels_recorded_at",
                  "manual_onchain_levels")
    op.drop_table("manual_onchain_levels")
