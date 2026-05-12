"""multi-tenancy Phase 7: tenant_telegram_links for per-tenant unlock notifications

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-12

Adds the table that maps a tenant to their Telegram chat. Used by
PR 3 (Telegram unlock-link flow) so the bot can DM a tenant a
short-lived deeplink when it boots without an unlocked K — tenant
clicks the link, enters passphrase on the web, bot resumes trading.

1:1 mapping (one Telegram chat per tenant) — simpler than 1:many
and matches the "personal bot DM" beta UX. Can revisit if users
need multi-device.

Reversible: downgrade drops the table outright. No data
backfill — links are recreated by the user-initiated /link flow.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_telegram_links",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # BIGINT because Telegram chat_id can be negative (group
        # chats) and exceeds INT32 for some user-IDs since the
        # Telegram-API expansion in 2024. Personal chats are
        # positive; -100xxxxx for supergroups.
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        # Display-only — Telegram lets users change usernames, so
        # don't use this as an identifier. Nullable because not
        # all chats have a username (e.g. private group with no
        # @ handle).
        sa.Column("telegram_username", sa.String(64), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("last_unlock_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Index on chat_id so the bot's /link handler can look up
    # "which tenant owns this chat" in O(log n). Without this the
    # bot's reverse-lookup at every command dispatch would be a
    # full scan.
    op.create_index(
        "idx_tenant_telegram_links_chat",
        "tenant_telegram_links",
        ["telegram_chat_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_tenant_telegram_links_chat",
        table_name="tenant_telegram_links",
    )
    op.drop_table("tenant_telegram_links")
