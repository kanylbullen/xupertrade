"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-27

Initial schema matching the tables created by SQLAlchemy create_all.
On a fresh database this migration creates all tables.
On an existing database run: alembic stamp head
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("size", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("ix_trades_mode", "trades", ["mode"])
    op.create_index("ix_trades_strategy_name", "trades", ["strategy_name"])
    op.create_index("ix_trades_symbol", "trades", ["symbol"])
    op.create_index("ix_trades_timestamp", "trades", ["timestamp"])
    op.create_index("ix_trades_is_paper", "trades", ["is_paper"])

    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("size", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("is_open", sa.Boolean(), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_positions_is_open", "positions", ["is_open"])
    op.create_index("ix_positions_is_paper", "positions", ["is_paper"])
    op.create_index("ix_positions_mode", "positions", ["mode"])
    op.create_index("ix_positions_strategy_name", "positions", ["strategy_name"])
    op.create_index("ix_positions_symbol", "positions", ["symbol"])

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("total_equity", sa.Float(), nullable=False),
        sa.Column("available_balance", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_equity_snapshots_is_paper", "equity_snapshots", ["is_paper"])
    op.create_index("ix_equity_snapshots_mode", "equity_snapshots", ["mode"])
    op.create_index("ix_equity_snapshots_timestamp", "equity_snapshots", ["timestamp"])

    op.create_table(
        "strategy_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("params_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("strategy_configs")
    op.drop_index("ix_equity_snapshots_timestamp", "equity_snapshots")
    op.drop_index("ix_equity_snapshots_mode", "equity_snapshots")
    op.drop_index("ix_equity_snapshots_is_paper", "equity_snapshots")
    op.drop_table("equity_snapshots")
    op.drop_index("ix_positions_symbol", "positions")
    op.drop_index("ix_positions_strategy_name", "positions")
    op.drop_index("ix_positions_mode", "positions")
    op.drop_index("ix_positions_is_paper", "positions")
    op.drop_index("ix_positions_is_open", "positions")
    op.drop_table("positions")
    op.drop_index("ix_trades_is_paper", "trades")
    op.drop_index("ix_trades_timestamp", "trades")
    op.drop_index("ix_trades_symbol", "trades")
    op.drop_index("ix_trades_strategy_name", "trades")
    op.drop_index("ix_trades_mode", "trades")
    op.drop_table("trades")
