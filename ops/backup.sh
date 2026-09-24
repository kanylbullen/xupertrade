#!/usr/bin/env bash
# Nightly off-host backup of Postgres and Redis (roadmap NU-2).
#
# What it does, in order:
#   1. pg_dump -Fc of the `hypertrade` database, plus the cluster's roles
#      (pg_dumpall --globals-only --no-role-passwords): the dump GRANTs to
#      the tenant_<hex> roles, and a restore into a fresh cluster fails
#      without them.
#   2. BGSAVE in Redis, waits for it to finish, copies the RDB out.
#   3. Sends all of it with restic (encrypted client-side) to the external
#      repository, then applies retention: 14 daily, 8 weekly, 6 monthly.
#   4. Pings healthchecks.io: /start when it begins, the bare URL on
#      success, /fail on any failure. A missed success ping is what alerts.
#
# Every secret comes from the environment, never from this repo or a file
# it names. Run it through Phase so they only exist for the one run:
#
#   RESTIC_REPOSITORY   e.g. b2:<bucket>:<path>          (required)
#   RESTIC_PASSWORD     repository encryption password   (required)
#   B2_ACCOUNT_ID       B2 application key ID    } required for a b2: repo;
#   B2_ACCOUNT_KEY      B2 application key       } restic reads them itself
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY      the same, for an s3: repo
#   HC_PING_URL         https://hc-ping.com/<uuid>       (required)
#
# Optional, non-secret:
#   BACKUP_STAGING_DIR      where the files are assembled (default
#                           /var/backups/xupertrade); emptied after every run
#   BACKUP_COMPOSE_PROJECT  compose project to find the containers in
#                           (default hypertrade)
#   BACKUP_PG_CONTAINER / BACKUP_REDIS_CONTAINER   container names, to skip
#                           the lookup by compose label
#   BACKUP_TAG / BACKUP_HOSTNAME   restic tag and host (default xupertrade);
#                           ops/restore-test.sh filters on the same values
#
# Host crontab (root; the host clock is UTC). Phase finds the app through
# the .phase.json in the checkout, hence the cd:
#
#   PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
#   30 3 * * * cd /opt/hypertrade && phase run -- ./ops/backup.sh >> /var/log/xupertrade-backup.log 2>&1
#
# Restore procedure and one-time setup: docs/runbooks/disaster-recovery.md.

set -euo pipefail
# Never trace: an inherited `bash -x` would print the ping URL.
{ set +x; } 2>/dev/null
umask 077

readonly DB_NAME=hypertrade
readonly DB_USER=postgres
readonly KEEP_DAILY=14 KEEP_WEEKLY=8 KEEP_MONTHLY=6
readonly REDIS_BGSAVE_TIMEOUT=300

STAGING_DIR="${BACKUP_STAGING_DIR:-/var/backups/xupertrade}"
COMPOSE_PROJECT="${BACKUP_COMPOSE_PROJECT:-hypertrade}"
TAG="${BACKUP_TAG:-xupertrade}"
SNAP_HOST="${BACKUP_HOSTNAME:-xupertrade}"
# The file names are the contract with ops/restore-test.sh.
readonly STAGED_FILES=(hypertrade.dump globals.sql redis-dump.rdb manifest.txt)

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# Ping healthchecks. The URL goes to curl on stdin (-K -), not argv, so it
# never shows up in `ps`; curl's own errors name the host, not the path.
hc_ping() {
    local suffix="$1"
    [[ -n "${HC_PING_URL:-}" ]] || return 0
    printf 'url = "%s%s"\n' "${HC_PING_URL%/}" "$suffix" \
        | curl -fsS -m 10 --retry 3 -o /dev/null -K - \
        || log "WARN: healthchecks ping '${suffix:-success}' failed" >&2
}

# Only once we hold the lock: before that, the files may be another run's.
LOCKED=0
clean_staging() {
    local f
    [[ $LOCKED -eq 1 ]] || return 0
    for f in "${STAGED_FILES[@]}"; do
        rm -f -- "${STAGING_DIR:?}/$f"
    done
}

on_exit() {
    local rc=$?
    clean_staging
    if [[ $rc -eq 0 ]]; then
        hc_ping ""
        log "OK"
    else
        hc_ping "/fail"
        log "FAILED (exit $rc)" >&2
    fi
    exit "$rc"
}

# Exactly one running container for a compose service, or die. $2 names
# the variable that skips the lookup, for the error message.
find_container() {
    local service="$1" override="$2" names
    names="$(docker ps --format '{{.Names}}' \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --filter "label=com.docker.compose.service=$service")"
    [[ -n "$names" ]] || die "no running '$service' container in compose project '$COMPOSE_PROJECT'"
    [[ "$(grep -c . <<<"$names")" -eq 1 ]] \
        || die "more than one '$service' container; set $override"
    printf '%s\n' "$names"
}

# First five bytes of a file, to check a format's magic.
magic() { head -c 5 -- "$1"; }

redis_cli() { docker exec "$REDIS_CONTAINER" redis-cli "$@" | tr -d '\r'; }
redis_info() { redis_cli INFO persistence | sed -n "s/^$1://p"; }

# A fresh RDB: wait out any save already running, remember LASTSAVE, start
# our own BGSAVE and wait until LASTSAVE moves past the remembered value.
# The one-second sleep keeps our save from finishing in the same second as
# the remembered one, which would leave LASTSAVE unchanged.
redis_fresh_rdb() {
    local deadline=$((SECONDS + REDIS_BGSAVE_TIMEOUT)) before reply dir file
    while [[ "$(redis_info rdb_bgsave_in_progress)" != 0 ]]; do
        [[ $SECONDS -lt $deadline ]] || die "a Redis background save never finished"
        sleep 2
    done
    before="$(redis_cli LASTSAVE)"
    sleep 1
    reply="$(redis_cli BGSAVE)"
    [[ "$reply" == *"Background saving started"* ]] || die "Redis BGSAVE refused: $reply"
    until [[ "$(redis_cli LASTSAVE)" -gt "$before" \
             && "$(redis_info rdb_bgsave_in_progress)" == 0 ]]; do
        [[ $SECONDS -lt $deadline ]] || die "Redis BGSAVE did not finish within ${REDIS_BGSAVE_TIMEOUT}s"
        sleep 2
    done
    [[ "$(redis_info rdb_last_bgsave_status)" == ok ]] || die "Redis BGSAVE failed (rdb_last_bgsave_status)"
    dir="$(redis_cli CONFIG GET dir | sed -n 2p)"
    file="$(redis_cli CONFIG GET dbfilename | sed -n 2p)"
    docker cp "$REDIS_CONTAINER:$dir/$file" "$STAGING_DIR/redis-dump.rdb" >/dev/null
    [[ "$(magic "$STAGING_DIR/redis-dump.rdb")" == REDIS ]] || die "copied RDB has no REDIS header"
}

# --- Preflight -----------------------------------------------------------

[[ -n "${HC_PING_URL:-}" ]] || die "HC_PING_URL is not set (run through 'phase run')"
trap on_exit EXIT
hc_ping "/start"

for var in RESTIC_REPOSITORY RESTIC_PASSWORD; do
    [[ -n "${!var:-}" ]] || die "$var is not set (run through 'phase run')"
done
case "$RESTIC_REPOSITORY" in
    b2:*) for var in B2_ACCOUNT_ID B2_ACCOUNT_KEY; do
              [[ -n "${!var:-}" ]] || die "$var is not set but the repository is b2:"
          done ;;
    s3:*) for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
              [[ -n "${!var:-}" ]] || die "$var is not set but the repository is s3:"
          done ;;
esac
for cmd in docker restic curl flock; do
    command -v "$cmd" >/dev/null || die "'$cmd' is not installed"
done
[[ "$STAGING_DIR" == /?* ]] || die "BACKUP_STAGING_DIR must be an absolute path other than /"

install -d -m 700 -- "$STAGING_DIR"
exec 9>"$STAGING_DIR/.lock"
if ! flock -n 9; then
    # Another run is mid-backup: leave its files and its check alone.
    trap - EXIT
    log "another backup run holds $STAGING_DIR/.lock; exiting" >&2
    exit 75
fi
LOCKED=1
clean_staging  # leftovers from a run that was killed

PG_CONTAINER="${BACKUP_PG_CONTAINER:-$(find_container postgres BACKUP_PG_CONTAINER)}"
REDIS_CONTAINER="${BACKUP_REDIS_CONTAINER:-$(find_container redis BACKUP_REDIS_CONTAINER)}"
[[ "$(redis_cli PING)" == PONG ]] || die "Redis in $REDIS_CONTAINER does not answer PING"

restic cat config >/dev/null \
    || die "cannot open the restic repository (not initialised yet? wrong password?)"

# --- Postgres ------------------------------------------------------------

log "pg_dump $DB_NAME from $PG_CONTAINER"
docker exec "$PG_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc \
    >"$STAGING_DIR/hypertrade.dump"
[[ "$(magic "$STAGING_DIR/hypertrade.dump")" == PGDMP ]] || die "pg_dump output has no PGDMP header"
docker exec "$PG_CONTAINER" pg_dumpall -U "$DB_USER" --globals-only --no-role-passwords \
    >"$STAGING_DIR/globals.sql"
alembic_rev="$(docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc \
    'SELECT version_num FROM alembic_version')"

# --- Redis ---------------------------------------------------------------

log "BGSAVE in $REDIS_CONTAINER"
redis_fresh_rdb
redis_keys="$(redis_cli DBSIZE)"

# --- Manifest (no secrets: times, versions, counts) ----------------------

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
{
    printf 'created_utc=%s\n' "$(date -u +%FT%TZ)"
    printf 'created_epoch=%s\n' "$(date -u +%s)"
    printf 'alembic_revision=%s\n' "$alembic_rev"
    printf 'redis_keys=%s\n' "$redis_keys"
    printf 'git_sha=%s\n' "$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf 'pg_dump=%s\n' "$(docker exec "$PG_CONTAINER" pg_dump --version)"
} >"$STAGING_DIR/manifest.txt"

# --- Off-host ------------------------------------------------------------

log "restic backup (alembic $alembic_rev, $redis_keys Redis keys)"
restic backup --host "$SNAP_HOST" --tag "$TAG" \
    "${STAGED_FILES[@]/#/$STAGING_DIR/}"
log "restic forget + prune (keep $KEEP_DAILY daily, $KEEP_WEEKLY weekly, $KEEP_MONTHLY monthly)"
restic forget --host "$SNAP_HOST" --tag "$TAG" \
    --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" \
    --prune
