"""funding_payments: dedupe on (tenant_id, mode, coin, timestamp), not hash

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-25

HyperLiquid's `userFunding` answers the same all-zero hash for every
funding event, so `UNIQUE(hash)` let exactly one funding row ever be
stored (2026-04-28): every later insert hit the constraint and was
dropped as a "duplicate". HL settles funding once per coin per hour per
account, so an event is identified by who (tenant, mode), what (coin)
and when (timestamp). `hash` stays as a plain column.

Nothing is deleted either way. Upgrade fails, rather than dedupes, if
two rows already share the new key (none can: the old key kept the
table at one row). Downgrade fails while two rows share a hash — true
after a single poll on 0017, so a live table cannot go back without
someone deciding which funding rows to throw away.
"""

from alembic import op


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


EVENT_KEY = "uq_funding_payments_event"
EVENT_KEY_COLUMNS = ["tenant_id", "mode", "coin", "timestamp"]
# What Postgres named 0003's unnamed `UniqueConstraint("hash")`, and
# create_all's `unique=True` alike. Dropped by name, without IF EXISTS:
# a table whose hash key is called something else fails here, loudly,
# instead of migrating with the old key still refusing every insert.
OLD_HASH_KEY = "funding_payments_hash_key"


def upgrade() -> None:
    op.drop_constraint(OLD_HASH_KEY, "funding_payments", type_="unique")
    op.create_unique_constraint(EVENT_KEY, "funding_payments", EVENT_KEY_COLUMNS)


def downgrade() -> None:
    op.drop_constraint(EVENT_KEY, "funding_payments", type_="unique")
    op.create_unique_constraint(OLD_HASH_KEY, "funding_payments", ["hash"])
