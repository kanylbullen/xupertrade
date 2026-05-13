"""tenant_secrets.expires_at for HL private-key rotation reminders

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-13

Nullable timestamp column. Today only populated by the dashboard
credentials UI for HYPERLIQUID_PRIVATE_KEY and
HYPERLIQUID_MAINNET_PRIVATE_KEY; other secret rows leave it NULL.

NULL = no expiry tracking, no Telegram reminders (legacy behavior).
"""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_secrets",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenant_secrets", "expires_at")
