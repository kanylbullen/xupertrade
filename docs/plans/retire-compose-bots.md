# PR 4 — Retire compose-defined bots

## Goal

Remove the three operator-side compose-defined bots (`bot-paper`,
`bot-testnet`, `bot-mainnet`) so that **every** bot in the system is
orchestrator-spawned per-tenant. This is the final step of the
multi-tenancy migration: one codepath in the bot, one codepath in
the dashboard, no env-injection fallback.

After this PR:
- Compose has only postgres, redis, dashboard, caddy, cloudflared.
- Every bot container is created via `dashboard/src/lib/bot-orchestrator.ts`.
- HL keys, Telegram tokens, etc. live exclusively in `tenant_secrets`
  (no more `HYPERLIQUID_*` / `TELEGRAM_*` env on the bot).
- `dashboard/src/lib/bot-api.ts:botFetch` (the legacy env-URL proxy)
  is deleted along with `BOT_API_URL_*` env vars.

## Pre-flight (must be true before merging)

These are operator-side checks, not code:

- [ ] Operator's tenant-bots are running for all 3 modes and have
      been trading for ≥ 24h without drift from compose-bots
- [ ] No open positions on operator's compose-bot-mainnet (real
      money risk; harder to migrate state cleanly)
- [ ] Operator has linked Telegram + verified `/link` flow works
- [ ] Operator has tested `Send unlock link to Telegram` round-trip
- [ ] Operator's tenant_secrets has `API_KEY` set (currently it
      comes from Phase env — once env-injection retires, the
      tenant-bot reads it from secrets like any tenant would, OR
      we keep API_KEY as an orchestrator-system-env since it's a
      shared bot↔dashboard secret, not per-tenant identity)

## Architecture decisions

### Where auth/tls config lives after retirement

Currently the bot proxies `/api/auth/config` + `/api/auth/configure` +
`/api/tls/config` + `/api/tls/configure` to its own handlers, which
read/write `dashboard:auth:*` and `dashboard:tls:*` Redis keys plus
env-first overrides (`AUTH_MODE`, `OIDC_*`, `TLS_*`).

The bot doesn't *consume* auth or TLS config itself — it just serves
the Redis keys to the dashboard via HTTP. This is leftover from
before the dashboard had direct DB+Redis access. With compose-bots
gone, the dashboard talks to Redis directly.

**Plan**: move the Redis read/write into dashboard-side code
(`dashboard/src/lib/auth-config.ts`, `dashboard/src/lib/tls-config.ts`).
The env-first override stays — but reads `process.env` on the
dashboard side instead of the bot side.

The 5 affected routes:
- `GET  /api/auth/config` → currently `botFetch`; new: read Redis directly
- `POST /api/auth/configure` → currently `botFetch`; new: write Redis
  directly + invalidate session-secret cache
- `POST /api/auth/login` → currently calls `${botUrl}/api/auth/verify`
  to check basic creds; new: bcrypt-compare against the hash stored in
  Redis directly (already there — `dashboard:auth:basic:hash`)
- `GET  /api/tls/config` → read Redis directly
- `POST /api/tls/configure` → write Redis + push Caddy config

### Caddy admin API from dashboard

Currently `bot/hypertrade/notify/caddy_admin.py:apply_caddy_config()`
builds a Caddy JSON config and POSTs to `http://caddy:2019/load`. The
dashboard reaches Caddy via the same internal Docker network
(`caddy:2019` resolves inside the compose network), so the dashboard
can do this directly.

**Plan**: port `caddy_admin.py` to TypeScript — pure JSON construction
+ fetch, no SDK needed. New file
`dashboard/src/lib/caddy-admin.ts`. The bot's existing code stays
(reachable for back-compat during migration; deleted in a follow-up
once the dashboard-side is verified live).

### Operator's API_KEY

Today: in Phase env, injected into both dashboard and (compose-)bots
as `API_KEY`. The dashboard forwards as `X-Api-Key`; bot's
`_require_auth` validates.

Per-tenant bots get the SAME `API_KEY` via `getOrchestratorSystemEnv()`
(PR 76). Operator's tenant-bots already work this way.

After this PR: nothing changes — `API_KEY` stays as a shared
bot↔dashboard secret in Phase + orchestrator system env. Not part of
tenant_secrets.

## Sub-PR breakdown

This is too big for one PR. Three sub-PRs:

### PR 4a — Port auth/tls to dashboard-direct (code-only, non-breaking)

- New `lib/auth-config.ts` with `getAuthConfig()` + `setAuthConfig()`
  reading/writing the `dashboard:auth:*` Redis keys directly + env-
  first override.
- New `lib/tls-config.ts` with `getTlsConfig()` + `setTlsConfig()`.
- New `lib/caddy-admin.ts` porting `apply_caddy_config()` from
  bot/hypertrade/notify/caddy_admin.py.
- Migrate 5 routes from `botFetch` to direct Redis:
  - `/api/auth/config`
  - `/api/auth/configure`
  - `/api/auth/login` (basic verify)
  - `/api/tls/config`
  - `/api/tls/configure`
- Tests for each new lib + integration tests for the routes.

**Non-breaking**: compose-bots can still serve the bot-side endpoints,
but no dashboard code calls them. The bot's TLS/auth handlers become
dead code (removed in 4c).

### PR 4b — Remove botFetch + BOT_API_URL env

- Delete `botFetch`, `botUrl`, `BOT_API_URL`, `URLS` from `bot-api.ts`.
- Delete `BOT_API_URL_*` env vars from `docker-compose.yml` dashboard
  service.
- Update remaining tests.

**Non-breaking** (assumes PR 4a is deployed and operator's
tenant-bots have been the canonical bots for ≥ 24h).

### PR 4c — Remove compose-bot services + env-injection (BREAKING — operator gates)

- Delete `bot-paper`, `bot-testnet`, `bot-mainnet` services from
  `docker-compose.yml`.
- Delete `HYPERLIQUID_*`, `TELEGRAM_*`, `VAULT_TRACKING_ADDRESS`,
  `MAINNET_ENABLED_STRATEGIES` from `x-bot-env`.
- Delete env-first override fields from `bot/hypertrade/config.py`
  (`auth_mode`, `oidc_*`, `tls_*` — moved to dashboard-side in 4a).
- Delete dead bot routes: `/api/auth/config`, `/api/auth/configure`,
  `/api/auth/verify`, `/api/auth/oidc-secret`,
  `/api/auth/session-secret`, `/api/tls/config`, `/api/tls/configure`.
- Delete `bot/hypertrade/notify/caddy_admin.py`.

**BREAKING**. Operator must:
1. Stop all compose-bots (`docker stop hypertrade-bot-{paper,testnet,mainnet}`)
2. Deploy PR 4c
3. Tenant-bots take over (already running from pre-flight checks)
4. After 24h stable, remove `HYPERLIQUID_*` / `TELEGRAM_*` /
   `VAULT_TRACKING_ADDRESS` / `MAINNET_ENABLED_STRATEGIES` /
   `AUTH_MODE` / `OIDC_*` / `TLS_*` from Phase (these are no longer
   read by anyone).

## Out of scope (separate PRs)

- Image rebrand `hypertrade-` → `xupertrade-`. Should happen AFTER
  PR 4c — once compose-bots are gone there are no pinned
  container_names to fight, and the orchestrator picks up the new
  image tag via `HYPERTRADE_BOT_IMAGE` env (rename to
  `XUPERTRADE_BOT_IMAGE` in same PR).
- Connection-error auto-reconcile (mark tenant_bots is_running=false
  on persistent connection failure with N-strikes counting). Not
  necessary for retirement; nice-to-have.
- Per-tenant Telegram routing (currently testnet-tenant-bot owns
  Telegram across all modes per tenant). Same model as today
  (compose testnet bot was the Telegram owner). Revisit if beta
  tenants want per-mode Telegram channels.

## Test plan

### PR 4a
- [ ] vitest: new lib + route tests pass
- [ ] tsc + next build clean
- [ ] After deploy: visit /options auth section, change basic creds,
      log out, log in with new creds — proves the full read-write-
      read cycle via dashboard-direct path
- [ ] After deploy: visit /options TLS section, toggle TLS, verify
      Caddy reloads (check Caddy admin API response)

### PR 4b
- [ ] vitest + tsc clean
- [ ] No `BOT_API_URL` references remain in src/
- [ ] After deploy: all dashboard pages still work

### PR 4c
- [ ] Operator pre-flight checks all pass (see top of doc)
- [ ] ZFS snapshot before deploy (CLAUDE.md policy)
- [ ] After deploy: tenant-bots continue trading without restart
- [ ] After 24h: drop Phase secrets

## Migration cookbook (for the operator)

```bash
# Pre-flight: tenant-bots running, no compose-bot positions

# 1. Snapshot
ssh root@192.168.12.228 'zfs snapshot rpool/...'

# 2. Stop compose-bots (PR 4c deploy assumes they're stopped)
ssh root@192.168.12.228 \
  'docker stop hypertrade-bot-paper hypertrade-bot-testnet hypertrade-bot-mainnet'

# 3. Deploy
ssh root@192.168.12.228 \
  "cd /opt/hypertrade && git fetch origin && git reset --hard origin/master && \
   phase run -- docker compose build --no-cache --pull dashboard && \
   phase run -- docker compose up -d --force-recreate dashboard"

# 4. Verify tenant-bots still trading
ssh root@192.168.12.228 \
  'docker ps --filter "label=hypertrade.tenant_id" --format "table {{.Names}}\t{{.Status}}"'

# 5. After 24h stable — drop Phase secrets:
#    HYPERLIQUID_PRIVATE_KEY, HYPERLIQUID_ACCOUNT_ADDRESS,
#    HYPERLIQUID_MAINNET_PRIVATE_KEY, HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS,
#    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED,
#    TELEGRAM_EVENTS, VAULT_TRACKING_ADDRESS, MAINNET_ENABLED_STRATEGIES,
#    AUTH_MODE, OIDC_*, TLS_*
```

## Estimated effort

- PR 4a: ~400 lines (3 new libs + 5 route migrations + tests)
- PR 4b: ~150 lines (deletions + test cleanup)
- PR 4c: ~200 lines (mostly compose deletions, some bot-side
  endpoint removals)

Total ~750 lines + plan doc. Spread over 2 sessions probably.
