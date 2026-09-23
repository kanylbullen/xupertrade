#!/usr/bin/env bash
# Set (or reset) the dashboard's basic-auth user from the host.
#
# This is the way back in when /login says "Authentication is locked":
# the stored auth mode is gone (Redis flushed, or its volume removed),
# nobody can sign in, and POST /api/auth/configure, the only in-app way
# to set the user, sits behind the same lock. It also works as a plain
# password reset.
#
# Usage (interactive; -t so the password prompt gets a terminal):
#   ssh -t -i ~/.ssh/hypertrade root@$DEPLOY_HOST /opt/hypertrade/scripts/set-basic-auth.sh
#
# What it does:
#   1. Looks up the operator tenant's sign-in identity
#      (`tenants.authentik_sub` of the operator row) and offers it as
#      the default username. A basic-auth username IS the tenant lookup
#      key, so any other name signs in as a new, empty tenant with no
#      operator rights.
#      If a different basic user is already stored, it shows that name
#      and asks before replacing it (default: no, nothing is written).
#   2. Prompts for the password twice, without echo. Every character
#      counts, leading and trailing whitespace included.
#   3. bcrypt-hashes it (cost 12, the dashboard's own @node-rs/bcrypt —
#      the same call POST /api/auth/configure makes) inside the
#      dashboard container, with the password on stdin.
#   4. Writes `dashboard:auth:basic:user` and `dashboard:auth:basic:hash`
#      in one MULTI/EXEC, fed to redis-cli on stdin. The stored mode:
#        - missing or unreadable (what makes the resolver answer
#          `locked`): also stores `basic`;
#        - `disabled`: warns that the dashboard is OPEN right now and
#          offers to switch it to `basic` (default: leave it). Builds
#          before #171 copied AUTH_MODE=disabled from Phase into Redis,
#          where it outlives the env var;
#        - `basic` / `oidc`: left alone.
#   5. Reads the keys back to confirm they landed.
#
# The password and the hash never appear in argv, in the output, or on
# disk. No restart is needed: the dashboard re-reads auth config within
# 30 seconds.
#
# Container names are discovered by compose service label. Override
# with DASHBOARD_CONTAINER / REDIS_CONTAINER / POSTGRES_CONTAINER.

set -euo pipefail

OPERATOR_TENANT_ID=00000000-0000-0000-0000-000000000001
KEY_MODE=dashboard:auth:mode
KEY_USER=dashboard:auth:basic:user
KEY_HASH=dashboard:auth:basic:hash

die() { echo "ERROR: $*" >&2; exit 1; }

# y/N prompt; anything but y/Y (including just Enter) is no.
confirm() {
    local answer
    read -r -p "$1 [y/N] " answer
    [[ $answer == [yY] ]]
}

find_container() {
    local service=$1 names count
    names=$(docker ps --filter "label=com.docker.compose.service=$service" --format '{{.Names}}')
    count=$(printf '%s' "$names" | grep -c . || true)
    [[ $count -eq 1 ]] || die "expected one running '$service' container, found $count — set ${service^^}_CONTAINER"
    printf '%s' "$names"
}

[[ -t 0 ]] || die 'needs an interactive terminal for the password prompt (ssh -t)'
command -v docker >/dev/null || die 'docker not found — run this on the dashboard host'

DASH=${DASHBOARD_CONTAINER:-$(find_container dashboard)}
REDIS=${REDIS_CONTAINER:-$(find_container redis)}

# Current state, read before anything is asked or written.
stored_mode=$(docker exec "$REDIS" redis-cli --raw GET "$KEY_MODE") \
    || die 'could not read Redis'
current_user=$(docker exec "$REDIS" redis-cli --raw GET "$KEY_USER") \
    || die 'could not read Redis'

# --- 1. username --------------------------------------------------------
operator_sub=''
if PG=${POSTGRES_CONTAINER:-$(find_container postgres 2>/dev/null)}; then
    operator_sub=$(docker exec "$PG" psql -U postgres -d hypertrade -tAc \
        "SELECT authentik_sub FROM tenants WHERE id = '$OPERATOR_TENANT_ID'" 2>/dev/null || true)
fi
if [[ -n $operator_sub ]]; then
    echo "The operator tenant signs in as: $operator_sub"
    echo 'Use that as the username to land in the operator tenant.'
    read -r -p "Username [$operator_sub]: " username
    username=${username:-$operator_sub}
else
    echo 'WARNING: could not read the operator tenant from Postgres.' >&2
    echo "The username must equal tenants.authentik_sub of $OPERATOR_TENANT_ID," >&2
    echo 'or sign-in lands in a new, empty tenant.' >&2
    read -r -p 'Username: ' username
fi
# Letters, digits and . _ @ + - only: covers emails and Authentik
# usernames, and keeps the value safe to pass through redis-cli's
# stdin command parser without quoting.
[[ $username =~ ^[A-Za-z0-9._@+-]{1,128}$ ]] \
    || die 'username must be 1-128 characters of A-Z a-z 0-9 . _ @ + -'
if [[ -n $operator_sub && $username != "$operator_sub" ]]; then
    echo 'WARNING: that is not the operator identity — sign-in will not reach the operator tenant.' >&2
fi
# There is one basic user. Setting a different name replaces the
# current one, which then can no longer sign in.
if [[ -n $current_user && $current_user != "$username" ]]; then
    echo "A different basic user is already set: $current_user"
    confirm "Replace it with $username? ($current_user will no longer be able to sign in)" \
        || die 'left the existing basic user unchanged; nothing written'
fi

# --- 2. password --------------------------------------------------------
# IFS= so leading/trailing whitespace is kept: it is part of the
# password the operator will type at /login.
IFS= read -r -s -p 'Password: ' password; echo
IFS= read -r -s -p 'Repeat password: ' password2; echo
[[ $password == "$password2" ]] || die 'passwords do not match'
unset password2
[[ ${#password} -ge 12 ]] || die 'password must be at least 12 characters'
# bcrypt ignores everything past 72 bytes; refuse rather than truncate.
bytes=$(printf '%s' "$password" | LC_ALL=C wc -c)
[[ $bytes -le 72 ]] || die 'password must be at most 72 bytes (bcrypt limit)'

# --- 3. hash inside the dashboard container ------------------------------
# printf is a builtin, so the password never becomes an argv entry.
# The node program itself holds nothing secret.
hash=$(printf '%s' "$password" | docker exec -i "$DASH" node -e '
  const chunks = [];
  process.stdin.on("data", (c) => chunks.push(c));
  process.stdin.on("end", async () => {
    const { hash } = require("@node-rs/bcrypt");
    process.stdout.write(await hash(Buffer.concat(chunks).toString("utf8"), 12));
  });
') || die 'hashing failed inside the dashboard container'
unset password
[[ $hash =~ ^\$2[aby]\$12\$[./A-Za-z0-9]{53}$ ]] \
    || die 'dashboard returned something that is not a cost-12 bcrypt hash'

# --- 4. write -----------------------------------------------------------
case $stored_mode in
    basic|oidc) set_mode='' ;;
    disabled)
        set_mode=''
        {
            echo
            echo '!!! WARNING: the stored auth mode is "disabled" — the dashboard is OPEN'
            echo '!!! to anyone who can reach it, with or without this user, including the'
            echo "!!! operator tenant's trades and positions. Builds before #171 copied"
            echo '!!! AUTH_MODE=disabled from Phase into Redis, where it outlives the env var.'
            echo
        } >&2
        if confirm 'Switch the stored mode to basic (sign-in required)?'; then
            set_mode=basic
        fi
        ;;
    *) set_mode=basic ;;
esac

{
    printf 'MULTI\n'
    printf 'SET %s %s\n' "$KEY_USER" "$username"
    printf 'SET %s %s\n' "$KEY_HASH" "$hash"
    [[ -n $set_mode ]] && printf 'SET %s %s\n' "$KEY_MODE" "$set_mode"
    printf 'EXEC\n'
} | docker exec -i "$REDIS" redis-cli >/dev/null || die 'Redis write failed'

# --- 5. verify ----------------------------------------------------------
[[ $(docker exec "$REDIS" redis-cli --raw GET "$KEY_USER") == "$username" ]] \
    || die "verification failed: $KEY_USER did not land"
[[ $(docker exec "$REDIS" redis-cli --raw GET "$KEY_HASH") == "$hash" ]] \
    || die "verification failed: $KEY_HASH did not land"
unset hash

echo "Basic-auth user set: $username"
if [[ -n $set_mode ]]; then
    echo "Stored auth mode was '${stored_mode:-<missing>}'; set it to '$set_mode'."
elif [[ $stored_mode == disabled ]]; then
    echo 'WARNING: stored auth mode left at "disabled" — the dashboard is still OPEN.' >&2
    echo "  To require sign-in later: docker exec $REDIS redis-cli SET $KEY_MODE basic" >&2
else
    echo "Stored auth mode '$stored_mode' left unchanged."
fi

env_mode=$(docker exec "$DASH" printenv AUTH_MODE 2>/dev/null || true)
case ${env_mode//[[:space:]]/} in
    '') ;;
    basic) ;;
    oidc) echo 'Note: AUTH_MODE=oidc is set in the dashboard env; use the "sign in with username + password" link on /login.' ;;
    disabled) echo 'WARNING: AUTH_MODE=disabled is set in the dashboard env — the dashboard is OPEN to anyone until it is removed from Phase.' >&2 ;;
    *) echo 'WARNING: AUTH_MODE in the dashboard env is not basic | oidc | disabled — the dashboard stays locked until it is fixed in Phase.' >&2 ;;
esac
echo 'Takes effect within 30 seconds; no restart needed.'
