#!/usr/bin/env bash
# Run Alembic migrations against the live database.
# Usage (from the bot/ directory):
#   DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head
#
# Or via docker compose:
#   docker compose run --rm bot-testnet sh /app/scripts/migrate.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL is not set" >&2
  exit 1
fi

echo "Running: alembic upgrade head"
uv run alembic upgrade head
echo "Done."
