"""add backtest_runs table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_name", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("days", sa.Float(), nullable=False),
        sa.Column("initial_equity", sa.Float(), nullable=False),
        sa.Column("final_equity", sa.Float(), nullable=False),
        sa.Column("total_return_pct", sa.Float(), nullable=False),
        sa.Column("apr", sa.Float(), nullable=False),
        sa.Column("sharpe", sa.Float(), nullable=False),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=False),
        sa.Column("num_trades", sa.Integer(), nullable=False),
        sa.Column("num_round_trips", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Float(), nullable=False),
        sa.Column("fees_paid", sa.Float(), nullable=False),
        sa.Column("position_size_usd", sa.Float(), nullable=False),
        sa.Column("fee_rate", sa.Float(), nullable=False),
        sa.Column("slippage_bps", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backtest_runs_strategy_name", "backtest_runs", ["strategy_name"])
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_created_at", "backtest_runs")
    op.drop_index("ix_backtest_runs_strategy_name", "backtest_runs")
    op.drop_table("backtest_runs")
