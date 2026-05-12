"""tenant_telegram_links: make telegram_chat_id globally unique

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-12

Tighten the schema introduced in 0012: a single Telegram chat must
belong to at most one tenant. Copilot caught that 0012 only added a
non-unique index, so `get_tenant_id_for_telegram_chat` could return
an arbitrary row if multiple tenants linked the same chat (e.g. a
shared phone, accidental re-use of someone else's chat id during
testing).

Promote the existing `idx_tenant_telegram_links_chat` index to a
UNIQUE one. Downgrade reverts to the non-unique form.

Note: if data already violates uniqueness on prod when this runs,
the migration will fail. There's no data on prod yet (PR 3a was
released < 24h ago and no tenants have linked), so we don't bother
with a dedup pre-step here.
"""

from alembic import op


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "idx_tenant_telegram_links_chat",
        table_name="tenant_telegram_links",
    )
    op.create_index(
        "idx_tenant_telegram_links_chat",
        "tenant_telegram_links",
        ["telegram_chat_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_tenant_telegram_links_chat",
        table_name="tenant_telegram_links",
    )
    op.create_index(
        "idx_tenant_telegram_links_chat",
        "tenant_telegram_links",
        ["telegram_chat_id"],
    )
