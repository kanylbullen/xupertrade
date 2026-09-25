# Dashboard auth recovery (`locked`)

Operator runbook, moved from `CLAUDE.md` § 3 on 2026-09-24. Every command
here touches the production host; an agent runs one only when the
operator asks for it in the session. Placeholders such as `$DEPLOY_HOST`
follow `CLAUDE.md` § 0 — never commit real values.

`lib/auth-config.ts:resolveMode` answers `locked` when it can't tell how
the installation authenticates: the stored `dashboard:auth:mode` is gone
(Redis flushed, or the `redisdata` volume removed) and no basic user or
OIDC config survived on a database that has tenants (or can't be read;
an empty one is the fresh install below), or a stored/env mode is not a
real mode. Every page redirects to `/login`, which explains this instead
of rendering data. **`POST /api/auth/configure` cannot fix it** — while locked,
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

**A fresh install with nothing configured resolves to `disabled`, and
nobody can sign in.** Since #182, alembic 0011 migrates an empty
database without the operator tenant row (it used to fail and roll back,
and the install booted `locked`). With no `AUTH_MODE`, no stored mode, no
basic user or OIDC config and no tenant, `resolveMode` answers
`disabled`. That is not an open first boot: there is no operator row
for a page to resolve, so pages bounce to `/login`; its form is refused
(`/api/auth/login` refuses in `disabled`); API routes answer 401 without
a session; and `POST /api/auth/configure` needs a signed-in operator.
Nothing in the dashboard gets you out, so bootstrap from outside it.
Both of these, before the first sign-in:

- **Configure sign-in:** `AUTH_MODE=oidc` plus the three `OIDC_*`
  values in Phase before the first `up`, or path 3 once the stack is
  running.
- **Create the operator tenant row** after `alembic upgrade head`, with
  `authentik_sub` set to the OIDC `sub` or the basic username you will
  sign in with:
  ```bash
  ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
    "docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c \
     \"INSERT INTO tenants (id, authentik_sub, email, display_name, is_operator, multi_bot_enabled)
       VALUES ('00000000-0000-0000-0000-000000000001', '<oidc-sub-or-basic-username>',
               'you@example.com', 'Operator', true, true);\""
  ```
  `docker exec`, not `docker compose exec`: compose reads the whole
  file first and stops on the `${POSTGRES_PASSWORD:?}` it requires
  unless run under `phase run --`. That id is the operator tenant
  everywhere (alembic 0011, `lib/tenant-server.ts`,
  `scripts/set-basic-auth.sh`). A first sign-in without the row creates
  an ordinary, non-operator tenant for that identity, and the `INSERT`
  then fails on the unique `authentik_sub`. For basic, insert the row
  before running path 3, so `set-basic-auth.sh` offers that username as
  its default.

Once the row exists, the install counts as provisioned: with no sign-in
configured it resolves to `locked`, not `disabled`, and paths 2 and 3
above get you in.

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
