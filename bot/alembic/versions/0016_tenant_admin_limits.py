"""tenants: max_active_bots / max_active_strategies / allowed_strategies

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-14

Operator-set per-tenant policy controls for the new /admin page.
All three columns are NULLable; NULL preserves current behavior
(no cap, no allowlist) on existing rows. Validated at the dashboard
write site, not via DB constraints, so the operator can later widen
the bounds without a migration.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("max_active_bots", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("max_active_strategies", sa.Integer(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "allowed_strategies",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "allowed_strategies")
    op.drop_column("tenants", "max_active_strategies")
    op.drop_column("tenants", "max_active_bots")
