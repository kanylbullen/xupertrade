#!/usr/bin/env bash
# Run Alembic migrations against the database in DATABASE_URL.
# Usage, from a checkout with the bot's dependencies installed (the script
# cds into bot/ itself):
#   DATABASE_URL=postgresql+asyncpg://... bot/scripts/migrate.sh
#
# On the host there is no bot service in docker-compose.yml any more, and
# scripts/ is not copied into the image, so run alembic from the bot image
# on the compose network instead (phase run supplies POSTGRES_PASSWORD):
#   phase run -- bash -c 'docker run --rm --network hypertrade_default \
#     -e DATABASE_URL="postgresql+asyncpg://postgres:$POSTGRES_PASSWORD@postgres:5432/hypertrade" \
#     --entrypoint /app/.venv/bin/alembic xupertrade-bot:latest upgrade head'
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: DATABASE_URL is not set" >&2
  exit 1
fi

echo "Running: alembic upgrade head"
uv run alembic upgrade head
echo "Done."
