#!/usr/bin/env bash
# Rotate the postgres user's password to whatever is currently in
# Phase under POSTGRES_PASSWORD. Idempotent: re-running with the
# same Phase value just no-ops.
#
# Place this on the host at /opt/hypertrade/scripts/rotate-postgres-password.sh
# and chmod +x.
#
# Usage (from /opt/hypertrade on the host):
#   phase run -- ./scripts/rotate-postgres-password.sh
#
# Full rotation (generate fresh value first):
#   phase secrets update POSTGRES_PASSWORD --random base64url --length 32
#   phase run -- ./scripts/rotate-postgres-password.sh
#   phase run -- docker compose up -d --no-deps --force-recreate dashboard
#   # then operator must Stop+Start each tenant bot via Settings -> Bots
#
# Auth model:
#   - The READ check (does the live DB already accept the Phase value?)
#     uses TCP via a sibling postgres-client container, which forces
#     scram-sha-256 — the same auth path the dashboard uses. Using
#     `docker exec ... psql` is unsafe here because pg_hba.conf has
#     `local trust` for socket connections, so socket auth succeeds
#     regardless of the password — making the idempotency check a
#     constant true (false positive that bit us 2026-05-14).
#   - The WRITE (ALTER USER) uses socket-trust (`docker exec ... psql`)
#     which doesn't require knowing the current password. So even if
#     the live DB password is unknown / out of sync with Phase, this
#     script can still set it to the Phase value.

set -euo pipefail

[[ -n "${POSTGRES_PASSWORD:-}" ]] || {
    echo 'ERROR: POSTGRES_PASSWORD not in env. Run via: phase run -- '"$0" >&2
    exit 1
}

# base64url charset only (A-Za-z0-9_-). Reject any other char so we
# can safely embed the value in a SQL literal without escaping.
[[ "$POSTGRES_PASSWORD" =~ ^[A-Za-z0-9_-]+$ ]] || {
    echo 'ERROR: POSTGRES_PASSWORD contains chars outside [A-Za-z0-9_-].' >&2
    echo 'Re-generate via: phase secrets update POSTGRES_PASSWORD --random base64url --length 32' >&2
    exit 1
}

CONTAINER=hypertrade-postgres-1
NETWORK=hypertrade_default
# Same image tag as the running postgres so the client is guaranteed
# wire-compatible. Pinning to :16-alpine keeps it lean.
PG_CLIENT_IMAGE=postgres:16-alpine

echo 'Idempotency check: does the running DB already accept the Phase value via TCP?'
TCP_RC=0
TCP_OUTPUT=$(docker run --rm --network "$NETWORK" \
       -e PGPASSWORD="$POSTGRES_PASSWORD" \
       "$PG_CLIENT_IMAGE" \
       psql -h postgres -U postgres -d hypertrade -tAc 'SELECT 1' 2>&1) || TCP_RC=$?
if [[ $TCP_RC -eq 0 ]]; then
    echo 'Phase value already authenticates via TCP — nothing to rotate.'
    exit 0
fi
# Only proceed to ALTER if the failure is a genuine auth mismatch.
# Any other failure (network, DNS, image-pull, postgres-down, wrong port)
# would otherwise cause us to silently rewrite the password via the
# socket-trust path and then fail verification, leaving the operator
# confused and dependent services pinned to the old DATABASE_URL.
if ! grep -q 'password authentication failed' <<<"$TCP_OUTPUT"; then
    echo 'ERROR: TCP check failed for a non-auth reason. Aborting before ALTER.' >&2
    echo "$TCP_OUTPUT" >&2
    exit 1
fi

echo 'Phase value does NOT authenticate via TCP — rotating now.'

# ALTER via socket-trust path (pg_hba's `local trust`). This works
# without knowing the current password.
#
# The SQL is fed via stdin (heredoc) instead of `-c "$SQL"` so the
# password literal never appears in argv — argv is visible to anyone
# with `ps`, `docker top`, or process-audit tooling. The base64url
# char-validation above remains the SQL-injection guard, since
# heredocs don't escape SQL string literals.
docker exec -i "$CONTAINER" \
    psql -U postgres -d hypertrade -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER USER postgres WITH PASSWORD '$POSTGRES_PASSWORD';
SQL
echo 'ALTER USER applied via socket-trust auth.'

# Verify by re-running the TCP auth check.
docker run --rm --network "$NETWORK" \
    -e PGPASSWORD="$POSTGRES_PASSWORD" \
    "$PG_CLIENT_IMAGE" \
    psql -h postgres -U postgres -d hypertrade -tAc 'SELECT 1' >/dev/null
echo 'Verified: Phase value now authenticates via TCP.'

echo
echo 'Next steps:'
echo '  1. phase run -- docker compose up -d --no-deps --force-recreate dashboard'
echo '  2. Operator: Stop+Start each tenant bot via Settings -> Bots'
