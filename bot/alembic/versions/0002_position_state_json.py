"""add positions.state_json column for exact strategy-state restore

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-29

Adds a nullable Text column for storing strategy-internal state (SL, TP,
entry, etc.) at signal time. On restart, restore_from_json() reads this
to avoid recomputation drift.
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("state_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "state_json")
