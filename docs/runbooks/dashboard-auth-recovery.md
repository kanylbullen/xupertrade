# Dashboard auth recovery (`locked`)

Operator runbook, moved from `CLAUDE.md` § 3 on 2026-09-24. Every command
here touches the production host; an agent runs one only when the
operator asks for it in the session. Placeholders such as `$DEPLOY_HOST`
follow `CLAUDE.md` § 0 — never commit real values.

`lib/auth-config.ts:resolveMode` answers `locked` when it can't tell how
the installation authenticates: the stored `dashboard:auth:mode` is gone
(Redis flushed, or the `redisdata` volume removed) and no basic user or
OIDC config survived, or a stored/env mode is not a real mode. Every
page redirects to `/login`, which explains this instead of rendering
data. **`POST /api/auth/configure` cannot fix it** — while locked,
`proxy.ts` redirects every non-public path to `/login`, that route
included, even for a valid session — and there is **no env
var for a basic user**: `getAuthConfig` reads only `AUTH_MODE` and
`OIDC_*` from env. If `AUTH_MODE` in Phase is itself a typo, fix it
there first — env wins over everything below. Otherwise any one of
these gets you back in:

1. **Restore Redis** from its `dump.rdb` (in the `redisdata` volume).
   Brings back everything else Redis held too — session secret, per-bot
   API keys, disabled-strategy sets — so prefer it when a snapshot
   exists.
2. **OIDC from Phase:** set `AUTH_MODE=oidc`, `OIDC_ISSUER`,
   `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` in Phase, then recreate the
   dashboard (`phase run -- docker compose up -d --no-deps
   --force-recreate dashboard` — `--no-deps` as in
   [deploy.md](deploy.md), or compose may recreate redis too). `lib/phase-sync.ts` copies them into Redis on boot.
3. **Basic user from the host:**
   ```bash
   ssh -t -i ~/.ssh/hypertrade root@$DEPLOY_HOST /opt/hypertrade/scripts/set-basic-auth.sh
   ```
   Prompts for a username (defaulting to the operator tenant's
   `authentik_sub` — a basic username *is* the tenant lookup key, so any
   other name signs in as a new, empty, non-operator tenant) and a
   password, hashes it with the dashboard's own bcrypt inside the
   dashboard container, and writes `dashboard:auth:basic:{user,hash}`
   (plus `dashboard:auth:mode=basic` if the stored mode is missing or
   unreadable). Asks before replacing a different existing basic user,
   and warns — offering to switch to `basic` — if the stored mode is
   `disabled`. Never prints the password or hash; no restart needed.
   Also the way to reset a forgotten basic password.

**A fresh install boots `locked` too.** The resolver only answers
`disabled` for a database with no tenants, and that never happens:
alembic 0011 refuses to run without the operator tenant row, and before
migrations the tenant probe fails, which counts as "tenants exist". So
configure sign-in for the first boot the same way — `AUTH_MODE=oidc`
plus the three `OIDC_*` values in Phase before the first `up` (sign in
with the identity whose `sub` equals the operator row's
`authentik_sub`), or path 3 once the stack is running. `.env.example`
lists the variables.

**Don't use `AUTH_MODE=disabled` as the way out.** It opens every page,
the operator tenant's trades and positions included, to anyone who can
reach the dashboard for as long as it is set. Since #171 it is
env-only — `phase-sync.ts` never copies `disabled` into Redis — so it
stops applying once removed, but it is not a recovery path, and not a
bootstrap one either: `POST /api/auth/configure` needs a signed-in
operator, and `disabled` signs nobody in.

**Check for a leftover stored `disabled`.** Builds before #171 copied
*any* `AUTH_MODE` into Redis on every boot, so an install that ever
booted with `AUTH_MODE=disabled` still has `dashboard:auth:mode=disabled`
stored — and stays open after the env var is gone:

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "docker exec hypertrade-redis-1 redis-cli GET dashboard:auth:mode"
```

`basic` or `oidc` is fine. If it says `disabled` and that is not a
deliberate choice, the dashboard is open to anyone right now: make sure
a way to sign in exists first (a basic user — `scripts/set-basic-auth.sh`
offers the switch itself — or the `OIDC_*` config), then
`docker exec hypertrade-redis-1 redis-cli SET dashboard:auth:mode basic`
(or `oidc`). Takes effect within 30 seconds. Setting it without a
working sign-in path locks everyone out, operator included.
