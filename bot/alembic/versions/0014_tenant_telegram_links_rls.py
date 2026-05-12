"""tenant_telegram_links: enable RLS + tenant_isolation policy

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-12

PR 3a created tenant_telegram_links; PR 3b granted tenant roles
SELECT/INSERT/UPDATE/DELETE on it so the bot's /link handler can
upsert. Copilot caught that without RLS, tenant A's bot role can
read ALL rows (every tenant's chat_ids + usernames) — a real
cross-tenant info leak.

Same pattern as alembic 0010 (which enabled RLS on the 9
per-tenant data tables). Uses the existing app_tenant_id() helper
to decode tenant_id from the role name; the bot's per-tenant role
matches automatically.

Superuser (postgres) keeps bypassing RLS — operator's
dashboard-side queries (e.g. cleanup, audit) continue to work.
"""

from alembic import op


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_telegram_links ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON tenant_telegram_links
          FOR ALL
          USING (tenant_id = app_tenant_id())
          WITH CHECK (tenant_id = app_tenant_id());
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation ON tenant_telegram_links;"
    )
    op.execute(
        "ALTER TABLE tenant_telegram_links DISABLE ROW LEVEL SECURITY;"
    )
