#!/usr/bin/env bash
# Restore test for the ops/backup.sh snapshots (roadmap NU-2, monthly).
#
# Restores the newest snapshot into a throwaway postgres:16 container that
# has no network at all, then checks what a real restore would need:
#   - the snapshot holds all four files, with the right magic bytes
#   - roles + pg_restore --exit-on-error complete without an error
#   - `alembic current` (run from the bot image) reads a revision, and it
#     is the one the backup recorded in its manifest
#   - row counts for trades, positions and tenants, and at least 1 tenant
#   - the snapshot is at most RESTORE_MAX_AGE_HOURS old (default 26)
# and prints one line: "RESTORE-TEST PASS ..." or "RESTORE-TEST FAIL: ...".
# Exit status 0 on PASS, 1 on FAIL.
#
# It never touches the live containers, volumes or networks: it removes
# only the containers it started itself (and their anonymous volumes)
# and the temp dir it made.
#
# Environment (secrets through 'phase run', as for ops/backup.sh):
#   RESTIC_REPOSITORY, RESTIC_PASSWORD, and the storage credentials
#   (B2_ACCOUNT_ID/B2_ACCOUNT_KEY or AWS_*), read by restic itself
#   RESTORE_TEST_HC_PING_URL   optional healthchecks URL: pinged on PASS,
#                              /fail on FAIL
#   RESTORE_PG_IMAGE           default postgres:16
#   RESTORE_BOT_IMAGE          default xupertrade-bot:latest (for alembic)
#   RESTORE_MAX_AGE_HOURS      default 26
#   BACKUP_TAG / BACKUP_HOSTNAME   must match ops/backup.sh (default xupertrade)
#
# Run by hand, or monthly from the host crontab:
#   0 5 1 * * cd /opt/hypertrade && phase run -- ./ops/restore-test.sh >> /var/log/xupertrade-restore-test.log 2>&1

set -euo pipefail
{ set +x; } 2>/dev/null
umask 077

PG_IMAGE="${RESTORE_PG_IMAGE:-postgres:16}"
BOT_IMAGE="${RESTORE_BOT_IMAGE:-xupertrade-bot:latest}"
MAX_AGE_HOURS="${RESTORE_MAX_AGE_HOURS:-26}"
TAG="${BACKUP_TAG:-xupertrade}"
SNAP_HOST="${BACKUP_HOSTNAME:-xupertrade}"
readonly DB_NAME=hypertrade DB_USER=postgres

log() { printf '%s restore-test: %s\n' "$(date -u +%FT%TZ)" "$*"; }

hc_ping() {
    local suffix="$1"
    [[ -n "${RESTORE_TEST_HC_PING_URL:-}" ]] || return 0
    printf 'url = "%s%s"\n' "${RESTORE_TEST_HC_PING_URL%/}" "$suffix" \
        | curl -fsS -m 10 --retry 3 -o /dev/null -K - \
        || log "WARN: healthchecks ping '${suffix:-success}' failed" >&2
}

# Unique per run (UTC second + PID): the names are how cleanup finds the
# containers this run started, and the only ones it ever removes.
pg_name="xupertrade-restore-test-$(date -u +%Y%m%dT%H%M%SZ)-$$"
alembic_name="$pg_name-alembic"
pg_started=0
alembic_started=0
work=""

cleanup() {
    if [[ $alembic_started -eq 1 ]]; then
        docker rm -f "$alembic_name" >/dev/null 2>&1 || true
    fi
    if [[ $pg_started -eq 1 ]]; then
        # -v: the image's anonymous data volume goes with it.
        docker rm -f -v "$pg_name" >/dev/null \
            || log "WARN: could not remove container $pg_name" >&2
    fi
    if [[ -n "$work" && -d "$work" ]]; then
        rm -rf -- "$work"
    fi
}
trap cleanup EXIT

# FAIL is final: print the verdict, ping, exit (the trap cleans up).
fail() {
    printf 'RESTORE-TEST FAIL: %s\n' "$*"
    hc_ping "/fail"
    exit 1
}

psql_q() {
    docker exec "$pg_name" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tAc "$1"
}

for var in RESTIC_REPOSITORY RESTIC_PASSWORD; do
    [[ -n "${!var:-}" ]] || fail "$var is not set (run through 'phase run')"
done
for cmd in docker restic ${RESTORE_TEST_HC_PING_URL:+curl}; do
    command -v "$cmd" >/dev/null || fail "'$cmd' is not installed"
done
docker image inspect "$BOT_IMAGE" >/dev/null 2>&1 \
    || fail "bot image $BOT_IMAGE not found (needed for alembic current)"

work="$(mktemp -d "${TMPDIR:-/tmp}/xupertrade-restore-test.XXXXXX")"

# --- Fetch the newest snapshot -------------------------------------------

# Resolve the ID first and restore exactly that one, so a backup landing
# in between cannot swap the snapshot under us.
snap="$(restic snapshots --host "$SNAP_HOST" --tag "$TAG" --latest 1 --compact \
    | awk '$1 ~ /^[0-9a-f]+$/ && length($1) == 8 { id = $1 } END { print id }')" \
    || fail "restic snapshots failed"
[[ -n "$snap" ]] || fail "no snapshot with host '$SNAP_HOST' and tag '$TAG'"
log "restoring snapshot $snap"
restic restore "$snap" --target "$work/restore" >/dev/null || fail "restic restore of $snap failed"

# Prints the one restored file with this name; fails (status 1) on none or
# several. Called in $(...), so it must not call fail itself.
find_one() {
    local found
    found="$(find "$work/restore" -type f -name "$1")"
    [[ -n "$found" && "$(grep -c . <<<"$found")" -eq 1 ]] || return 1
    printf '%s\n' "$found"
}
dump="$(find_one hypertrade.dump)" || fail "snapshot $snap: expected one hypertrade.dump"
globals="$(find_one globals.sql)" || fail "snapshot $snap: expected one globals.sql"
rdb="$(find_one redis-dump.rdb)" || fail "snapshot $snap: expected one redis-dump.rdb"
manifest="$(find_one manifest.txt)" || fail "snapshot $snap: expected one manifest.txt"
[[ "$(head -c 5 -- "$dump")" == PGDMP ]] || fail "hypertrade.dump has no PGDMP header"
[[ "$(head -c 5 -- "$rdb")" == REDIS ]] || fail "redis-dump.rdb has no REDIS header"
manifest_get() { sed -n "s/^$1=//p" "$manifest"; }

# --- Throwaway Postgres: no network, trust auth, nothing published --------

log "starting $PG_IMAGE as $pg_name (--network none)"
pg_started=1
docker run -d --name "$pg_name" --network none \
    --label xupertrade.restore-test=1 \
    -e POSTGRES_DB="$DB_NAME" -e POSTGRES_HOST_AUTH_METHOD=trust \
    "$PG_IMAGE" >/dev/null || fail "could not start $PG_IMAGE"

# The image first runs a socket-only server for initdb, then restarts; wait
# for TCP so we never restore into the one that is about to go away.
ready=0
for _ in $(seq 1 60); do
    if docker exec "$pg_name" pg_isready -q -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME"; then
        ready=1
        break
    fi
    sleep 1
done
[[ $ready -eq 1 ]] || fail "throwaway Postgres not ready after 60s"

docker cp "$globals" "$pg_name:/tmp/globals.sql" >/dev/null || fail "docker cp globals.sql failed"
docker cp "$dump" "$pg_name:/tmp/hypertrade.dump" >/dev/null || fail "docker cp hypertrade.dump failed"

# Roles first. "role \"postgres\" already exists" is expected here, so
# errors are not fatal; a missing role shows up in pg_restore instead.
docker exec "$pg_name" psql -q -h 127.0.0.1 -U "$DB_USER" -d postgres -f /tmp/globals.sql \
    >"$work/globals.log" 2>&1 || fail "applying globals.sql failed"
log "pg_restore"
if ! docker exec "$pg_name" pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
        --exit-on-error /tmp/hypertrade.dump >"$work/pg_restore.log" 2>&1; then
    tail -n 20 "$work/pg_restore.log" >&2
    fail "pg_restore failed"
fi

# --- alembic current, from the bot image, inside the same netns ----------

alembic_started=1
if ! docker run --rm --name "$alembic_name" --network "container:$pg_name" \
        -e DATABASE_URL="postgresql+asyncpg://$DB_USER@127.0.0.1:5432/$DB_NAME" \
        --entrypoint /app/.venv/bin/alembic "$BOT_IMAGE" current \
        >"$work/alembic.out" 2>"$work/alembic.err"; then
    tail -n 20 "$work/alembic.err" >&2
    fail "alembic current failed"
fi
revision="$(awk 'NF { r = $1 } END { print r }' "$work/alembic.out")"
[[ -n "$revision" ]] || fail "alembic current printed no revision"
expected="$(manifest_get alembic_revision)"
[[ "$revision" == "$expected" ]] \
    || fail "alembic current is '$revision' but the backup recorded '$expected'"
at_head="no"
grep -q '(head)' "$work/alembic.out" && at_head="yes"

# --- Row counts ----------------------------------------------------------

declare -A count
for table in trades positions tenants; do
    count[$table]="$(psql_q "SELECT count(*) FROM $table")" || fail "count(*) on $table failed"
    [[ "${count[$table]}" =~ ^[0-9]+$ ]] || fail "count(*) on $table returned '${count[$table]}'"
done
[[ "${count[tenants]}" -ge 1 ]] || fail "restored database has no tenants"

# --- Age, last so that a stale snapshot is still restore-tested ----------

created_epoch="$(manifest_get created_epoch)"
[[ "$created_epoch" =~ ^[0-9]+$ ]] || fail "manifest has no created_epoch"
age_hours=$(( ($(date -u +%s) - created_epoch) / 3600 ))
[[ $age_hours -le $MAX_AGE_HOURS ]] \
    || fail "newest snapshot $snap is ${age_hours}h old (limit ${MAX_AGE_HOURS}h); is the nightly backup running?"

printf 'RESTORE-TEST PASS snapshot=%s created=%s age=%sh alembic=%s head=%s trades=%s positions=%s tenants=%s redis_keys=%s\n' \
    "$snap" "$(manifest_get created_utc)" "$age_hours" "$revision" "$at_head" \
    "${count[trades]}" "${count[positions]}" "${count[tenants]}" "$(manifest_get redis_keys)"
hc_ping ""
