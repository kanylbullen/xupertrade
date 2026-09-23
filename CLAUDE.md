# HyperTrade — Agent Development Framework

This file is the operating manual for any Claude agent working on this repo.
The mission is to ship and maintain a **production-grade autonomous crypto
trader** that executes its 22 registered strategies faithfully, recovers from
failure, and never silently diverges from exchange reality.

The agent acts independently: investigates issues, fixes them, writes tests,
deploys, and verifies. It only stops to ask the user when an action is
destructive or genuinely ambiguous.

---

## 0. Personal info & secrets policy — READ EVERY COMMIT

**This repository is public.** Every commit goes to GitHub where anyone can
read it forever — even if you delete the file in a later commit, the value
lives in history. **Never commit any of these:**

| Type | Example | Where it goes instead |
|---|---|---|
| Telegram bot token | `8639592584:AAGj…` (digits, colon, 35 base64 chars) | Phase secrets manager (see § 3) |
| HyperLiquid private key | `0x` + 64 hex chars | Phase secrets manager |
| Cloudflare API token | 40-char base64 | Redis (`dashboard:tls:cf_token`) via Options page |
| OIDC client secret | provider-specific | Redis (`dashboard:auth:oidc_client_secret`) |
| `API_KEY` for the bot HTTP API | random string | Phase secrets manager |
| Phase service token | base64 | only on the host's `~/.phase/` config; NEVER in repo |
| Personal email used live | `you@yourdomain.com` | Phase (`TELEGRAM_*`, OIDC config); generic placeholder in docs (`you@example.com`) |
| Telegram chat ID | 8-12 digit number used as your identity | Phase secrets manager |
| Server IP / private LAN | `192.168.x.x`, `10.x.x.x` | `~/.ssh/config`, env vars (`$DEPLOY_HOST`, `$DEPLOY_IP`) |
| Real production hostname | `mybot.example.com` | local env / SSH config; use `$DEPLOY_HOST` placeholder in docs |
| Phase / secrets-manager URL | `secrets.example.com` | local env; use `$PHASE_URL` placeholder in docs |
| Wallet addresses (HL trading account) | `0x` + 40 hex | not needed in repo; bot reads from Phase |
| Holdings / position sizes that identify you | "I have 400 VVV" in commit msg | discuss in chat, not in commits |

**Before EVERY commit**, a pre-commit hook (see § Setup) blocks the diff
when known secret-shaped strings appear. **Never bypass with `--no-verify`**
unless the match is genuinely a false positive AND you've manually verified
the value isn't sensitive.

**Even with the hook, you (or a future agent) are still responsible.** The
hook catches known patterns, not novel ones. When writing docs, prefer:

- `$DEPLOY_HOST`, `$DEPLOY_IP`, `$YOUR_DOMAIN` placeholders
- `you@example.com`, `1234567890:your-actual-token-here` for examples
- `~/.ssh/<keyname>` for SSH key paths (not `/home/<user>/.ssh/...`)

**If you discover a secret that's already in history:**
1. **Rotate the credential immediately** (Telegram: `/revoke` to BotFather; HL: regenerate API wallet; CF: revoke the token in CF dashboard).
2. Mask the value in `HEAD` and commit + push the mask.
3. Optionally: rewrite history with `git filter-repo --replace-text` to scrub from older commits. Destructive — coordinate before doing it. Even then, anyone who already cloned has the old value.
4. Add the leaked pattern to the pre-commit hook's blocklist so it can't recur.

### Setup the hook (one-time, per local clone)

```bash
git config --local core.hooksPath .githooks
chmod +x .githooks/pre-commit
```

The hook is checked into `.githooks/pre-commit`. Setup is per-clone because
git ignores `core.hooksPath` from a checked-in `.git/config`.

---

## 1. Mission and definition of done

**Mission:** Build and maintain a HyperLiquid autotrader that
1. Executes the 22 registered strategies with byte-fidelity to their TradingView ports (where a port exists — `vvv_hedge` and `ath_breakout` are in-house designs, not ports).
2. Survives network outages, exchange errors, DB hiccups, and process restarts without losing position state.
3. Produces accurate trade records, equity snapshots, and PnL — DB always matches exchange reality.
4. Surfaces problems via Telegram and the dashboard before they become silent losses.

**The agent cannot guarantee a strategy is profitable.** It *can* and *must*:
- Guarantee the strategy logic matches the source PineScript.
- Detect and remove strategies that are demonstrably broken (e.g. SL=0 bug, sign flips, unit confusion).
- Recommend disabling strategies whose live behavior diverges from their backtest archetype, with evidence (live trade history, signal-vs-execution comparison).

**Definition of done for any task:**
- Code change is committed with a clear message.
- Pushed to `origin/master`.
- Deployed to the testnet server (`root@$DEPLOY_HOST`, `/opt/hypertrade/`).
- Verified live: relevant logs are clean, dashboard shows expected state, or `curl` to the bot API confirms behavior.
- If a bug was fixed: a test or runtime check exists that would have caught it.

---

## 2. Repo layout (the parts that matter)

```
~/xupertrade/
├── bot/
│   ├── hypertrade/
│   │   ├── main.py                  # entry point — auto-instantiates every registered strategy
│   │   ├── config.py                # pydantic-settings (.env), tolerates unknown vars
│   │   ├── api.py                   # aiohttp HTTP API: control, positions, indicator-status, hodl, vaults, hyperliquid diagnostic (see § 3 for the route list — no auth/tls/oidc endpoints; those were dashboard-side and retired)
│   │   ├── engine/
│   │   │   ├── runner.py            # tick loop: heartbeat, periodic reconcile (5min), funding poll (30min), flip-detect, signal exec
│   │   │   ├── control.py           # Redis-backed state: paused, disabled, leverage, allow_multi_coin, heartbeat
│   │   │   ├── portfolio.py         # kill-switch read + daily-PnL persistence (Redis-backed; see Open — Medium for its fail-open gaps)
│   │   │   └── indicators_status.py # per-strategy live "what is each strategy seeing right now"
│   │   ├── exchange/
│   │   │   ├── base.py              # Exchange interface (Position, Balance, Order, OrderType)
│   │   │   ├── paper.py             # in-memory simulated exchange
│   │   │   └── hyperliquid.py       # live HL via SDK + tenacity retry on reads only
│   │   ├── strategies/              # 22 registered strategies (14 Pine ports + 6 new ports + vvv_hedge, ath_breakout custom) + meta/<name>.json descriptors. `golden_cross.py` also lives here but is deliberately NOT registered (backtested, lags buy-and-hold badly — kept for reference only, see registry.py's comment).
│   │   ├── hodl/                    # advisory "add to your spot stack now" signals — NOT trading strategies, place no orders. altseason/btc_accumulation_zone/btc_ath_breakout/hype_accumulation/macro_backdrop/vault_picks + registry.py; surfaced on the dashboard /hodl page
│   │   ├── vaults/                  # HyperLiquid vault scanner — daily catalog poll → filter → Sharpe/max-DD/ROI metrics → vault_snapshots row; read-only, no funds deposited/withdrawn; surfaced on /vaults
│   │   ├── reconcile/               # fills.py: reconcile_fills_from_hl — fetches HL fill history to price/attribute reconcile-driven closes
│   │   ├── data/                    # candle feed (REST + WS) with retry/backoff
│   │   ├── events/                  # Redis pub/sub event bus
│   │   ├── notify/
│   │   │   ├── telegram.py          # notifier + command bot (/status /strategies /positions /eval /kelly /today /flat)
│   │   │   └── caddy_admin.py       # builds + applies Caddy JSON config (HTTPS or self-signed)
│   │   ├── reports/
│   │   │   └── weekly_eval.py       # /eval and /kelly engine; CLI: `python -m hypertrade.reports.weekly_eval`. Posts to Telegram/stdout only — does not write a file (see § 8).
│   │   ├── backtest/
│   │   │   ├── runner.py            # replays candles, simulates fills+fees, returns BacktestResult
│   │   │   ├── metrics.py           # pure functions: Sharpe, APR, max DD, periods/year
│   │   │   └── __main__.py          # CLI: `python -m hypertrade.backtest --strategy X --days N`
│   │   └── db/
│   │       ├── models.py            # Trade, PositionRecord, EquitySnapshot, StrategyConfig, FundingPayment, BacktestRun, TenantBot
│   │       └── repo.py              # all SQL + reconcile_positions (closes orphans both sides)
│   ├── tests/                       # pytest, pytest-asyncio (756 passed, 1 skipped, 3 xfailed as of 2026-09-15)
│   ├── alembic/versions/            # 16 migrations: 0001 initial → 0016 tenant_admin_limits (see dir for current head)
│   ├── scripts/migrate.sh
│   └── Dockerfile
│
├── dashboard/                       # Next.js 16 (App Router, Turbopack)
│   └── src/
│       ├── app/                     # /overview/[mode], /trades, /strategies, /backtests, /hodl, /vaults, /options,
│       │                            # /settings/bots, /settings/credentials, /unlock, /login, /admin/server,
│       │                            # /admin/[tenantId] (operator-only), /api/... — /status 308-redirects to /settings/bots
│       ├── proxy.ts                 # Next 16 proxy.ts (was middleware.ts) — auth gate
│       ├── components/              # PositionCard, IndicatorStatus, BotControls, AuthConfig, TlsConfig, MultiCoinToggle, ...
│       └── lib/
│           ├── bot-api.ts           # mode-aware bot API proxy (adds the per-bot X-Api-Key — see bot-api-key.ts)
│           ├── bot-api-key.ts       # generates + persists each tenant bot's unique API key in Redis (`tenant:bot:<botId>:api_key`)
│           ├── bot-orchestrator.ts  # tenant_bots row + decrypted secrets → Docker container spec; create/start/stop via docker.ts
│           ├── docker.ts            # thin dockerode wrapper (bind-mounted host socket) — create/start/stop/remove/inspect/list only
│           ├── tenant.ts            # tenant session/identity helpers used by client + server code
│           ├── tenant-server.ts     # server-only tenant resolution (requireTenantServer etc.)
│           ├── tenant-pg-role.ts    # per-tenant Postgres role provisioning for RLS-style isolation
│           ├── operator.ts          # operator-only auth/role checks (gates /admin/*)
│           ├── crypto/              # passphrase.ts (Argon2id + verifier), secrets.ts (AES-256-GCM), k-cache.ts (Redis-backed K cache) — see README "Security model"
│           ├── admin/               # limits.ts (per-tenant caps), server-stats.ts, strategy-names.ts
│           ├── session-store.ts     # HMAC-signed session cookie read/write backed by Redis
│           ├── rate-limit.ts        # Redis-pipeline rate limiter (login, unlock, passphrase attempts)
│           ├── heartbeat-watchdog.ts # polls each running tenant bot's heartbeat; DMs Telegram directly when one goes stale (a dead bot can't alert on its own death)
│           ├── telegram-alert.ts    # thin Telegram Bot API client used by the watchdog
│           ├── db.ts                # Drizzle, same Postgres as bot
│           ├── queries.ts           # PnL aggregations: per-strategy, daily, totals
│           ├── auth.ts              # HMAC-signed session cookies, fetchAuthConfig with 30s cache
│           └── oidc.ts              # openid-client config + state-bundle codec + resolveRedirectUri
│
├── caddy/
│   ├── Dockerfile                   # caddy:2-builder + caddy-dns/cloudflare plugin
│   └── Caddyfile                    # bootstrap: self-signed HTTPS for $CADDY_HOST + HTTP→HTTPS redirect
│
├── tv-source/                       # 1:1 with strategies — <name>.pine for source-vs-port audits
├── docker-compose.yml               # postgres + redis + dashboard + caddy + cloudflared (profile `public`) + bot-image (profile `build`, produces xupertrade-bot:latest — no bot service runs here, see below)
├── README.md                        # user-facing docs
├── .env.example
├── AGENTS.md                        # fast entry point for coding agents (defers to this file)
└── CLAUDE.md                        # this file
```

**There is no bot service in `docker-compose.yml`** (retired in PR #89; the
`bot-paper`/`bot-testnet`/`bot-mainnet` compose services described in older
revisions of this file no longer exist). `bot-image` is profile-gated
(`build`) and only produces the `xupertrade-bot:latest` image — it has no
`command` and never runs as a service. Actual bot containers are spawned per
tenant per mode by the dashboard: `dashboard/src/lib/bot-orchestrator.ts`
turns a `tenant_bots` DB row + that tenant's decrypted secrets into a Docker
container spec, and `dashboard/src/lib/docker.ts` (a thin dockerode wrapper
over the bind-mounted host socket) creates/starts/stops it. Container names
follow `xupertrade-bot-<16-hex tenant short id>-<mode>` (e.g.
`xupertrade-bot-3a2f1e4caaaa1111-mainnet`); they publish no host ports and
are reached over the compose network. **Telegram is env-driven per bot
instance** (`TELEGRAM_ENABLED`) — the orchestrator hardcodes it to `true`
only for the `mainnet` bot and `false` for `paper`/`testnet`
(`bot-orchestrator.ts`), so in the current deployment Telegram runs on the
**mainnet** bot, not testnet.

---

## 3. Operating environment

- **Local working tree:** `~/xupertrade/` (Linux, x86_64).
- **Remote server:** `root@$DEPLOY_HOST` at `$DEPLOY_IP`, code at `/opt/hypertrade/`. SSH key: `~/.ssh/hypertrade`. Always log in as `root`. Concrete values for the maintainer's deployment are in their local `~/.bashrc` / SSH config — **never commit them here**.
- **Git remote:** `https://github.com/kanylbullen/xupertrade.git`, branch `master`.
- **Postgres:** runs in compose, published on **`127.0.0.1:5432` only**.
  Not reachable from the LAN. Use `docker compose exec -T postgres psql -U
  postgres -d hypertrade`, or tunnel for a GUI client:
  `ssh -L 15432:localhost:5432 root@$DEPLOY_HOST`.
- **Redis:** runs in compose, published on **`127.0.0.1:6379` only**. It
  runs with no `requirepass`, so reachability *is* the access control — it
  holds `dashboard:auth:session_secret` (the HMAC key signing session
  cookies), the OIDC client secret and the CF API token. **Never republish
  it beyond loopback without setting `requirepass` first** and threading it
  through both the dashboard and the bots.
- **Bot APIs:** paper `:8000`, testnet `:8001`, mainnet `:8002` **inside
  their containers only**. Orchestrator-spawned tenant bots publish no host
  ports at all (`HostConfig.PortBindings` is null); the dashboard reaches
  them by container name over the compose network. The bot image
  (`python:3.13-slim`) has **no `wget` or `curl`** — use Python instead, and
  most endpoints (`/api/positions`, `/api/control/heartbeat`, etc.) now
  require the bot's per-tenant `X-Api-Key` (see § 3 "is the bot OK?" check
  below for how to find it):
  ```bash
  docker exec <bot-container> python -c \
    'import urllib.request,sys; req=urllib.request.Request(sys.argv[1], headers={"X-Api-Key": sys.argv[2]}); print(urllib.request.urlopen(req).read().decode())' \
    http://localhost:<port>/api/positions "$API_KEY"
  ```
- **Dashboard:** `127.0.0.1:3000` (dev/debug + health probes) and via Caddy
  at `:443` (LAN) / Cloudflare tunnel (public). Both real ingress paths
  reach the container over the compose network, not the published port.
- **Caddy reverse proxy:** `:80` (HTTP→HTTPS redirect), `:443` (HTTPS), `:443/udp` (HTTP/3). Admin API on `:2019` (internal Docker network only).
- **HTTPS:** `https://$DEPLOY_HOST/` — Let's Encrypt cert auto-renewed by Caddy via Cloudflare DNS-01.
- **Auth:** username + password (basic) or OIDC. Configured under Options → Authentication. Bcrypt hashes + HMAC-signed session cookies stored in Redis. If `/login` says **Authentication is locked**, see "Dashboard auth recovery" below.

### Dashboard auth recovery (`locked`)

`lib/auth-config.ts:resolveMode` answers `locked` when it can't tell how
the installation authenticates: the stored `dashboard:auth:mode` is gone
(Redis flushed, or the `redisdata` volume removed) and no basic user or
OIDC config survived, or a stored/env mode is not a real mode. Every
page redirects to `/login`, which explains this instead of rendering
data. **Options → Authentication cannot fix it** — `/options` and
`/api/auth/configure` sit behind the same lock — and there is **no env
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
   dashboard (`phase run -- docker compose up -d --force-recreate
   dashboard`). `lib/phase-sync.ts` copies them into Redis on boot.
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
   unreadable). Never prints the password or hash; no restart needed.
   Also the way to reset a forgotten basic password.

**Don't use `AUTH_MODE=disabled` as the way out.** It opens every page,
the operator tenant's trades and positions included, to anyone who can
reach the dashboard for as long as it is set. It is deliberately
env-only — `phase-sync.ts` never copies `disabled` into Redis — so it
stops applying once removed, but it is not a recovery path.

### Secrets management — Phase

**Source of truth: a self-hosted [Phase](https://phase.dev) instance.**
All runtime secrets (HL keys, Telegram token+chat, API_KEY, public URL,
Caddy host, vault tracking address, mainnet allowlist) live there in the
`hypertrade` app's `Development` env. The host has the `phase` CLI
installed + authenticated via service token. **No `.env` file on the
host** — anything that previously lived there is now in Phase.

The deploy command wraps everything in `phase run` so secrets are
injected as env vars only for the lifetime of the docker-compose call —
they're never written to disk on the host.

To add or change a secret:
- Locally: `phase secrets create KEY=VALUE` or `phase secrets update KEY=VALUE`
- Or via the Phase web UI on the operator's instance
- Then redeploy (host pulls fresh values from Phase on the next `phase run`)

Bootstrap on a new host (one-time):
1. Create a service token in Phase UI → User Settings → Tokens
2. `scp .phase.json` from the operator's local clone to `/opt/hypertrade/`
   on the host (the file links the host to the operator's app/env IDs;
   it is NOT a secret but is gitignored because it's per-instance)
3. `ssh root@$DEPLOY_HOST 'cd /opt/hypertrade && phase auth --mode token'`
   (paste token)
4. Verify: `ssh root@$DEPLOY_HOST 'cd /opt/hypertrade && phase secrets list'`
   should show the keys

The Phase instance URL itself (operator's hostname for their Phase web
UI) is treated as private operator info — see § 0. Use `$PHASE_URL`
as a placeholder if a doc example needs to refer to it.

### Rotating POSTGRES_PASSWORD

`POSTGRES_PASSWORD` lives in Phase (app `xupertrade`, env `Development`).
Postgres only reads that env on **container init** — updating it in Phase
does NOT change the password inside an already-initialized database. Full
rotation is therefore a two-step dance: update the live DB user with
`ALTER USER` (authenticating with the OLD password, which is still active
in the running cluster), then recreate every container that has a
`DATABASE_URL` baked into its env so they pick up the new value.

> `docker-compose.yml` has required `POSTGRES_PASSWORD` via
> `${POSTGRES_PASSWORD:?...}` (compose-up fails fast if it's unset/empty)
> since PR #122 — both the postgres and dashboard services read the
> Phase-injected value, no hardcoded literal survives a `phase run --`
> deploy. The steps below are the live rotation dance.
>
> Tenant-bot containers spawned by the dashboard read `DATABASE_URL` from
> the dashboard's environment, so they inherit whatever the dashboard
> sees at spawn time.

Steps:

1. **Generate a fresh secret locally** (don't commit, don't paste in chat):
   ```bash
   openssl rand -base64 36 | tr -d '/+=\n'
   ```

2. **Update Phase**: web UI → app `xupertrade` → env `Development` → key
   `POSTGRES_PASSWORD` → save.

3. **ALTER inside the running postgres container**, authenticating with
   the OLD password (still active in `pg_authid` until you change it):
   ```bash
   ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
     "docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade \
      -c \"ALTER USER postgres WITH PASSWORD '<paste new>';\""
   ```

4. **Recreate consumers** so they pick up the new env-injected
   `DATABASE_URL`:
   ```bash
   ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
     "cd /opt/hypertrade && phase run -- docker compose up -d --no-deps --force-recreate dashboard"
   ```
   Each tenant bot must be Stop+Started via the dashboard UI
   (Settings → Bots → restart) so its container is respawned with the
   new env. Until that happens, already-running tenant bots keep using
   the OLD `DATABASE_URL` from their spawn-time env and will start
   failing auth on the next reconnect.

5. **Verify**:
   ```bash
   ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
     "docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c '\\du'"
   ```
   Should list the `postgres` role without auth error. Spot-check that
   the dashboard `/` and a tenant bot's `/api/positions` still respond.

The postgres CONTAINER itself does NOT need to be recreated — the user
password lives in `pg_authid` (durable WAL state) and `ALTER USER`
updates it in place. Recreating postgres would mean a
volume-preserving down/up, which is more disruption than needed and
risks DB downtime during active trading.

---

### Host-side cron jobs

These are installed manually on `$DEPLOY_HOST` (root crontab); they live
on the host, NOT in the repo. Documented here so a future agent knows
what's expected to be running and why.

| Schedule | Command | Why |
|---|---|---|
| `0 4 * * *` | `docker builder prune -af > /var/log/docker-prune.log 2>&1` | Docker's builder cache grows unbounded between deploys and fills the 30G root partition after ~5–10 dashboard rebuilds. Without this, deploys eventually fail with disk-full errors mid-COPY (see the cache-trap warning below). 04:00 UTC is well outside trading-hours volatility windows. |

To install:

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "( crontab -l 2>/dev/null; echo '0 4 * * * docker builder prune -af > /var/log/docker-prune.log 2>&1' ) | crontab -"
```

To verify after install: `ssh root@$DEPLOY_HOST 'crontab -l'`.

**Re-verify after every host rebuild, not just after install.** The root
crontab lives only on the host, not in any Docker volume or the repo — a
host rebuild (new VM, disk restore, provider migration) silently drops it.
Found missing entirely on 2026-09-15 (disk had climbed to 83% with 10+ GB
reclaimable and no prune had run). `crontab -l` costs one SSH round-trip;
run it as a standing item whenever you touch the host, not only right after
you install the job.

### Standard deploy command

**Always split build from `up -d` and verify image age between them.** The
chained one-liner (`build && up -d`) hit the layer cache and silently
produced stale images on three back-to-back PR deploys in a 24h window
(2026-05-12) — the chained command exited 0, dashboard "deployed", and
live behavior matched the pre-PR state because `--force-recreate` then
brought the container up off an unchanged image SHA. See the cache-trap
warning below for the underlying mechanic.

```bash
# 1. Pull master + check disk + build with --no-cache --pull
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && \
   git fetch origin && git reset --hard origin/master && \
   df -h / | tail -1 && \
   phase run -- docker compose --profile build build --no-cache --pull bot-image dashboard"

# 2. Verify image age — both timestamps must be newer than your commit
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "docker image inspect hypertrade-dashboard -f 'built: {{.Created}}' && \
   docker image inspect xupertrade-bot:latest -f 'built: {{.Created}}'"

# 3. Only THEN recreate the containers. `--no-deps` is not optional:
#    without it compose also recreates any dependency whose config
#    changed (redis, postgres), and a recreated redis can come up empty.
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && phase run -- docker compose up -d --no-deps --force-recreate dashboard"
```

> ⚠️ **Never let compose recreate `redis` by accident.** The dashboard
> `depends_on` redis, so `up -d --force-recreate dashboard` *without*
> `--no-deps` also recreates redis whenever redis's compose config
> changed. PR #168 changed it (named `redisdata:/data` volume), and the
> first recreate after that starts on an **empty** volume. Redis holds
> the paused flags, the per-mode `disabled` strategy sets, leverage
> overrides, the per-bot API keys, the session secret, the auth config,
> the OIDC client secret and the CF token. An empty Redis reads as
> "not paused, nothing disabled", so every bot — mainnet included —
> resumes with every strategy enabled (auth itself fails closed to
> `locked`). Before any deploy that recreates redis: pause the bots,
> `docker exec hypertrade-redis-1 redis-cli SAVE`, `docker cp` the
> `dump.rdb` out, copy it into the target volume, and only then
> recreate redis on its own and check `DBSIZE` and the key list match.

> ⚠️ **`up -d --force-recreate dashboard` only refreshes the dashboard
> container.** Tenant-bots are orchestrator-spawned (separate Docker
> containers, not in `docker-compose.yml` post-PR-4c) and the orchestrator
> does NOT auto-restart them when `xupertrade-bot:latest` updates. So a
> bot code change ships the new image into the registry but already-running
> tenant bots keep using the read-only layers from the image they spawned
> against until the dashboard explicitly recreates them.
>
> When the bot code itself changed (not just the dashboard), the
> operator must trigger a tenant-bot restart per running bot via the
> dashboard UI's `Settings → Bots → restart`. Or via API:
> `POST /api/tenant/me/bots/<bot_id>/stop` then `/start`. Until that
> happens, the new bot logic only applies to bots STARTED after the
> deploy — not to ones already running.

**There is no per-mode compose profile or `bot-mainnet`/`bot-testnet`
service any more** (that model predates the multi-tenant orchestrator, see
§ 2). One `bot-image` build (profile `build`) produces
`xupertrade-bot:latest` for every mode; step 1 above (`--profile build
build --no-cache --pull bot-image dashboard`) already covers mainnet bots
too. To pick up new bot code, restart the running mainnet bot the same way
as any other tenant bot: dashboard UI `Settings → Bots → restart`, or
`POST /api/tenant/me/bots/<bot_id>/stop` then `/start`.

> ⚠️ **Cache trap on first build of a service that hasn't been built
> recently.** An old image with the same `name:tag` from a long-stopped
> previous build can silently win the layer cache, and a plain `docker
> compose build <service>` does NOT invalidate it — the build "succeeds"
> in seconds with zero COPY-step output but the resulting image is
> days/weeks old and missing your latest code. This bit us on the first
> mainnet boot 2026-05-10 (back when mainnet ran as its own compose
> service, `bot-mainnet`, before the orchestrator refactor): the image
> lacked the audit-C3 allowlist and all 21 strategies traded on real money
> for one tick before pause caught it. **Always force `--no-cache` on the
> first build of any service/image that hasn't been built recently:**
>
> ```bash
> phase run -- bash -c 'docker compose --profile build build --no-cache --pull bot-image'
> ```
>
> (`--pull` refreshes the base image too — combining both ensures the
> entire image is rebuilt from current sources.)
>
> When in doubt, look at `docker images | grep xupertrade-bot` — if the
> "CREATED" column says days/weeks ago, force a no-cache rebuild before
> restarting any bot off it.
>
> **This trap is not bot-image-specific** — it bit the dashboard the
> same way on 2026-05-11 during the healthcheck-fix iterations. Five
> back-to-back PRs rebuilt dashboard but `docker compose build --pull
> dashboard` hit the layer cache every time and never produced a new
> image SHA.
> `--force-recreate` then recreated the container off the same stale
> image, so the new code never reached production. Symptom: deploy
> command exits 0, container restarts, but live behavior matches the
> pre-PR state. After three rounds of "but I deployed it!" confusion,
> `docker image inspect hypertrade-dashboard -f '{{.Created}}'` showed
> the image was 2 days old. **Verify image age after every deploy when
> you change the dashboard or bot code:**
>
> ```bash
> docker image inspect hypertrade-dashboard -f 'built: {{.Created}}'
> ```
>
> If the timestamp pre-dates your commit, force a no-cache rebuild.
>
> **Disk-full mid-build is the silent corollary.** The 30G root partition
> fills up after ~5–10 dashboard rebuilds because docker's builder cache
> grows unbounded. When the disk fills mid-COPY, `docker compose build`
> emits a confusing error and may still exit 0 in the chained
> `build && up -d` form, leaving a half-baked or missing image. Run
> `docker builder prune -af` before deploying when `df -h /` shows the
> root partition above 80%. A daily host-side cron (see
> "Host-side cron jobs" above) keeps this from accumulating between
> deploys.

Build only what changed for speed — but keep `--no-cache --pull` and
the standalone build step (no chained `&& up -d`):

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && phase run -- docker compose --profile build build --no-cache --pull dashboard"
```

Then verify image age and recreate per the three-step shape above. The
old `bash -c 'build --pull && up -d'` form is what bit us with the
cache-trap — don't reintroduce it.

### Standard "is the bot OK?" check

Four things differ from what an older version of this section said:
`docker compose exec -T postgres` fails once `POSTGRES_PASSWORD` is
`${POSTGRES_PASSWORD:?}`-required unless wrapped in `phase run --`; the bot
image has no `wget`; `/api/positions` and `/api/control/heartbeat` now
require the bot's per-tenant `X-Api-Key` (generated by the orchestrator and
stored in Redis at `tenant:bot:<botId>:api_key`, keyed by the
`tenant_bots.id` UUID — not surfaced anywhere in the UI); and `docker logs`
is unreliable on a long-running bot container (see below).

```bash
# 0. Find the bot's DB id + container name, then its API key
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade \
   -c \"SELECT id, container_name, mode FROM tenant_bots WHERE mode='testnet';\""
# then, with that id:
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "docker exec hypertrade-redis-1 redis-cli GET tenant:bot:<bot-id>:api_key"

# 1. DB ↔ exchange parity
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "echo '=== Exchange ==='; docker exec \$(docker ps --format '{{.Names}}' \
   | grep -E '^xupertrade-bot-.*-testnet\$') python -c \
   'import urllib.request,sys; req=urllib.request.Request(sys.argv[1], headers={\"X-Api-Key\": sys.argv[2]}); print(urllib.request.urlopen(req).read().decode())' \
   http://localhost:8001/api/positions '<api-key>'; echo; \
   echo '=== DB ==='; docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade \
   -c \"SELECT strategy_name, symbol, side, size, entry_price FROM positions WHERE mode='testnet' AND is_open=true;\""

# 2. Recent errors. Container names follow `xupertrade-bot-<16-hex short
# id>-<mode>`; discover the right one before grepping logs.
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "matches=\$(docker ps --format '{{.Names}}' | grep -E '^xupertrade-bot-.*-testnet\$'); \
   case \$(echo \"\$matches\" | grep -c .) in \
     0) echo 'no testnet bot container running' >&2; exit 1 ;; \
     1) container=\$matches ;; \
     *) echo 'multiple testnet bots — pick one explicitly:' >&2; \
        echo \"\$matches\" >&2; exit 1 ;; \
   esac; \
   echo \"=== \$container ===\"; \
   docker logs \"\$container\" --since 1h 2>&1 | grep -iE 'error|warning' | tail -50"
```

**When `docker logs` fails with `invalid character '\x00'`** (seen on all
three bots after the 2026-09-01 reboot — long-running JSON-file logs can get
corrupted in place), `docker logs` is unusable; read the raw file instead:

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "logpath=\$(docker inspect -f '{{.LogPath}}' <container>); \
   sudo tail -c 2000000 \"\$logpath\" | grep -a -iE 'error|warning' | tail -50"
```

`tail -c` avoids trying to seek by line count through a file `docker logs`
already can't parse; `grep -a` forces text mode over any embedded binary
garbage near the corruption point.

---

## 4. Subagent strategy

Use subagents aggressively when the work is parallelizable, isolated, or
context-heavy. Spawn them with the lightest model that can do the job.

### When to delegate (and to which model)

| Task type | Subagent type | Model | Why |
|---|---|---|---|
| "Find every place where X happens" | `Explore` | sonnet (default) | Read-only, parallelizable, keeps main context lean |
| "Plan the implementation for Y" | `Plan` | sonnet | Architectural thinking before coding |
| "Run the test suite, report failures" | `general-purpose` | haiku | Mechanical, fast, cheap |
| "Code review this diff" | `general-purpose` | sonnet | Needs judgment but bounded scope |
| "Backtest strategy X on stored OHLCV" | `general-purpose` | sonnet | Self-contained Python work |
| "Audit all 14 strategies vs their PineScript sources" | `general-purpose` | sonnet, in parallel (one per strategy) | Embarrassingly parallel |
| "Investigate this production incident" | `general-purpose` | sonnet | Open-ended, needs flexibility |

**Defaults:**
- Use `sonnet` for anything that touches code or makes recommendations.
- Use `haiku` only for very mechanical tasks (running commands, parsing logs into a structured summary).
- Reserve `opus` for hard architectural decisions or hairy debugging across many files.

### Parallelization rule

If you need 3+ independent investigations, fire them in parallel in one message. Example: auditing 14 strategies → 14 parallel `general-purpose` agents, one per strategy file.

### Subagent prompt requirements

Subagents start cold. Always include:
1. The exact file paths involved.
2. What "done" looks like (a short report? a code change? a test run?).
3. Any constraints (don't deploy, don't write to DB, etc.).
4. The expected output format (bullet points, table, diff).

Bad: `"check the strategies"`. Good: `"Audit ~/hypertrade/bot/hypertrade/strategies/*.py against the TradingView source linked in each strategy's docstring. Report any logic divergences with file:line and the source line. Do not edit anything."`

---

## 5. Backlog

The agent owns this list. Update it as bugs are found and fixed. Move items
between sections as their state changes. Never delete an entry — fixed items
stay in **Done** with the commit hash so the agent has institutional memory.

### Open — Critical (blocks safe operation)

- [ ] **Reconcile flattens the book on a transient HyperLiquid read failure.**
  `hyperliquid.py`'s `get_positions()` catches every exception (including a
  bare HL `502 Bad Gateway`) and returns `[]`; the reconcile pass then reads
  that as "no exchange positions" and closes every open DB row as an orphan
  (`pnl=0`, no `Trade` row), and on its next 5-minute pass market-closes the
  still-real exchange position it now sees with no DB owner — again with no
  `Trade` row. Confirmed live: every `closed orphan` log line in the last two
  weeks of the testnet log is immediately preceded by an HL 502 traceback;
  48 of 155 testnet closes since 2026-05-29 (31%) match this pattern. See
  `bot/reports/analysis-2026-09-15.md` § 2 for the full trace and fix
  sketch. Fix in progress on `fix/reconcile-read-failure`.

### Open — High (impacts trading correctness)

(none currently)

### Open — Medium

- [ ] **Volatility-adjusted sizing (option C from Kelly discussion).** Replace fixed `MAX_POSITION_SIZE_USD` with ATR-normalized sizing: `notional = RISK_BUDGET_USD / (atr × atr_mult)` so every trade has roughly the same dollar-risk regardless of asset volatility. Industry standard, no statistical estimation needed. Add `RISK_BUDGET_USD` config; keep `MAX_POSITION_SIZE_USD` as a hard cap. ~3-4h work, defensive change. Pair with the Kelly report for guidance on the budget level.
- [ ] **Drawdown-based auto-scaling (option B from Kelly discussion).** Add `MAX_STRATEGY_DRAWDOWN_PCT` per strategy. When 80% of cap reached → halve effective margin until 7-day rolling PnL > 0. Limits exposure on degrading strategies without requiring stationary distribution assumptions like Kelly does.
- [ ] **Engine money-path hardening (analysis-2026-09-15 § 4, not covered by `fix/reconcile-read-failure`):**
  - `Order.size` on the HL exchange wrapper is the *requested* size, not the rounded size actually submitted — DB row and fee are computed from the wrong number (this is the live 247× "BTC size mismatch" reconcile warning).
  - `main.py` sets `repo = None` on a DB error at boot and enters the trading loop anyway; every `if self.repo` gate downstream (flip-detect, same-side dedup, coin/family gate, `MAX_TOTAL_EXPOSURE_USD`) silently no-ops, and `_check_parity_after_trade` then returns `True` unconditionally.
  - Kill-switch read failure and `set_daily_pnl` persist failure (`engine/portfolio.py`) both fail open — a Redis blip loses the daily-loss counter or lets a kill-switched bot keep trading.
  - `PositionRecord` (the `positions` table) has no `leverage` column; the exposure-cap check does `getattr(p, "leverage", 1)`, mixing full-notional and margin-divided-by-leverage units in the same cap.
  - A failed `meta()` fetch at HL-exchange construction leaves `_sz_decimals` empty and every coin silently rounds to 4dp; the bot boots "successfully" anyway.
  - A failed `update_leverage` push before an open is logged only — the open proceeds at whatever leverage HL already has for that coin.
  - The HL exchange's read-retry predicate keys off exception *type*, so 4xx `ServerError` gets retried like a transient 5xx (reads only, no double-submit risk, but wasted retry budget on a permanent error).
- [ ] **Dashboard auth/tenant-isolation hardening (analysis-2026-09-15 § 5)** — auth-mode fail-open when the Redis key is absent, an OIDC open-redirect (`safeNext` blocks `//` but not `/\`), and a few lower-severity gaps. Fix in progress on `fix/dashboard-auth-hardening`.

### Open — Low

(none currently)

### Done

> Last full sync: 2026-05-02. Earlier items first; recent work grouped
> by theme below the original list.

- [x] Cross-strategy position lock (`allow_multi_coin` Redis flag) — commit `4435c46`.
- [x] Dashboard shows exchange positions, not stale DB — commit `4435c46`.
- [x] Fast `/api/control/config` endpoint to avoid blocking on exchange calls — commit `ccd7c2a`.
- [x] `Repository.reconcile_positions()` on startup closes orphans — commit `b2bc62a`.
- [x] Manual DB cleanup of 2 ETH orphans + BTC size correction (testnet, 2026-04-29).
- [x] EMA-crossover `_sl=0.0` instant-close bug fixed with `None` sentinel.
- [x] Keltner-breakout missing-SL-after-restart fixed.
- [x] Telegram HTML escaping for reason/message strings — commit `ceb3a74`.
- [x] Unreachable `logger.info()` in `HyperLiquidExchange.__init__` — commit `b4ea004`.
- [x] `.env.example` and `dashboard/.env.example` complete — commit `b4ea004`.
- [x] Initial Alembic migration; bot still uses `create_all` for live, Alembic is manual.
- [x] Network retry/backoff via `tenacity` on HL reads + candle fetcher — commit `7731061`.
- [x] DB-driven close-size with sanity-clamp to exchange — commit `7731061`.
- [x] Heartbeat written every tick + `/api/control/heartbeat` endpoint — commit `7731061`.
- [x] Periodic reconcile every 5 minutes from runner loop — commit `8263c20`.
- [x] Daily PnL Telegram digest at 23:00 + `/today` command — commit `8263c20`.
- [x] `MAX_TOTAL_EXPOSURE_USD` cap across all open positions — commit `180f3b1`.
- [x] Reconcile size-mismatch threshold (no spam on HL rounding) — commit `180f3b1`.
- [x] `btc_mean_reversion` restore_state now sets `_take_profit` (was missing) — commit `c8c1e50`.
- [x] `supertrend` restore_state lazily recomputes SL/TP (was running unprotected) — commit `c8c1e50` + ATR-scope fix in `b8ab902`.
- [x] Test coverage 14/14 strategies (was 4/14) — commit `b8ab902`. 51 passed, 2 xfailed.
- [x] None-sentinel migration for hash_momentum, pivot_supertrend, moon_phases — commit `fdedff3`.
- [x] Strategy-state DB persistence (positions.state_json + export_state/restore_from_json on base; hash_momentum implements both) — commit `fdedff3`. Eliminates SL drift across restart.
- [x] Funding-cost tracking: `funding_payments` table, periodic poller (30 min), best-effort strategy attribution — commit `c19da09`. Verified live: first poll captured SOL short funding payment.
- [x] Weekly strategy evaluation report: `hypertrade.reports.weekly_eval`, scheduled Sunday 18:00 Telegram digest, on-demand `/eval [days]` command — commit `c19da09`.
- [x] Settings tolerates unknown env vars (`extra="ignore"`) — commit `c19da09`.
- [x] Overview: detailed P&L breakdown (totals, per-day with bars, per-strategy with W/L) — commit `39d1175`.
- [x] Funding cost merged into Daily P&L net (visualized as net = realized + funding) — commit `3b9c5e1`.
- [x] State persistence (export_state/restore_from_json) on all 8 stateful strategies — commit `3b9c5e1`. Restart now restores SL/TP/trail/entry verbatim from DB instead of recomputing.
- [x] Half-Kelly sizing report (advisory only — does NOT change live sizing). New `/kelly [days]` Telegram command + `weekly_eval --kelly` CLI flag. 6 unit tests cover edge cases (too few trades, no losses, classic 60% / 1:1, negative edge clamp, 40% / 3:1, avg_win/avg_loss).
- [x] Telegram /kelly HTML escape for `<10` literal — commit `ebc93ff`.
- [x] Dashboard authentication phase 1 (basic): bcrypt-hashed username/password stored in Redis, HMAC-signed session cookies (no external deps), Next.js 16 proxy.ts gates non-public routes, Options page UI, Sign Out in nav. — commit `2bcda54`. OIDC fields persist but enforcement reserved for phase 2.
- [x] OIDC phase 2: openid-client-based authorization-code flow with PKCE+state, /api/auth/oidc/start + /callback, mode toggle on Options, friendly error mapping, session subject from claims.email > preferred_username > sub. — commit `71f64cd`.
- [x] Engine flip-detection: when a strategy emits OPEN_X while DB shows opposite side, synthesize CLOSE-then-OPEN. — commit `92e68c2`.
- [x] Backtest framework with metrics (Sharpe / APR / max DD / win rate), CLI (`python -m hypertrade.backtest --all --days 180`), 14 unit tests. — commit `c338c03`. First 180d run: bb_short +5.5%, moon_phases +5.0%, volatility_breakout flat, 11 others negative — strong evidence ema_crossover (-12.4%) and penguin_volatility (-10.3%) are over-trading.

#### Audits & port fixes (2026-04-30 → 05-01)
- [x] Source-vs-Python audit of all 14 original strategies via parallel subagent. Found 4 HIGH-prio port bugs — commit `feb12d2`:
  - `ema_crossover` had a phantom reverse-on-opposite-cross block. Pine source only enters on signal, exits on SL. Removed the reversal → 180d backtest -12.4% → +5.2% APR (190 → 1 trades).
  - `penguin_volatility` hardcoded `use_timing_filter=True`; Pine default is False (state-based entries). Added the toggle, defaulted False to match Pine.
  - `volatility_breakout` latched SL from entry-time ATR; Pine recomputes every bar. Now matches Pine.
  - `hash_momentum` missed Pine's opposite-signal-close block. Added.
- [x] tv-source files renamed to `<strategy_name>.pine` for 1:1 mapping with bot/hypertrade/strategies/<name>.py — commit `feb12d2`.
- [x] Reconcile auto-closes exchange-side orphans — commit `a697242`. Without this, an untracked exchange position would silently absorb a strategy's next OPEN order via netting, corrupting both DB and exchange state. Now closes via market order with a dust threshold.

#### New strategies (2026-04-30 → 05-01)
- [x] 6 new ports via 3 parallel subagents — commit `7b44ecc`: `daily_long_0830` (15m time-of-day), `kalman_breakout` (Kalman+ATR bands, 1h ETH), `bb_rsi_scalper` (BB+RSI+EMA+Fib, 15m BTC), `hash_supertrend` (no-SL flip, 1h BTC), `oleg_aryukov` (6-indicator ensemble, 1h ETH), `qullamagi_breakout` (multi-MA breakout, 1h ETH). 21 new tests. None showed clear edge in 180d window.
- [x] `main.py` now auto-instantiates every registered strategy via `list_strategies()` — commit `3a8394d`. Was hardcoded to 14, so newly-registered strategies weren't actually active.
- [x] `vvv_hedge` — custom defensive hedge for staked VVV holdings — commits `db46764` + `03f7bea`. EMA-bearish mandatory filter; emits `Signal(size=400)` to bypass engine notional calc; symmetric exit on EMA flip. 5 unit tests. Backtest: 2 round trips on 144d uptrend (vs. 5 before EMA filter) — defensive by design.

#### Backtest persistence (2026-05-01)
- [x] `backtest_runs` table + Alembic migration 0004 — commit `36500a5`. CLI auto-saves every run with all summary metrics (APR, Sharpe, max DD, win rate, trade counts) plus input params (position_size, fee_rate, slippage_bps). Use `--no-save` to opt out. 37 historical runs persisted.

#### Dashboard authentication (2026-04-30 → 05-01)
- [x] Bitwarden / password-manager autofill: switched login + auth-config forms to uncontrolled refs and `autoComplete="current-password"` so password-manager DOM injection isn't discarded by React's controlled-input pattern. Form has `name="username"`/`name="password"` so managers identify the fields. — commits `b1707e6`, `b9c9014`, `927c366`.
- [x] Login redirect-to-overview after sign-in (sanitize `next` target) — commit `bd1aaf5`. `next` param falls back to `/` when missing, points at `/login`, or points at an `/api/*` route.
- [x] Logout 405 fix: AuthConfig "Sign out" was a `<a href>` triggering GET on a POST-only route. Replaced with a button that POSTs; added GET fallback that clears cookie and 302-redirects. — commit `347b64a`.
- [x] OIDC phase 2 with full PKCE+state flow — commit `71f64cd`. `openid-client` v6, `/api/auth/oidc/start` + `/callback` + Options-page mode toggle.
- [x] OIDC `redirect_uri` correctness in containerized prod — commit `8fada21`. Introduced `PUBLIC_URL` env (falls back to `DASHBOARD_URL`); `resolveRedirectUri()` helper used by start AND callback so token-exchange `redirect_uri` matches what was sent at auth time. Bot exposes scoped `GET /api/auth/oidc-secret` (API_KEY-only) so dashboard can fetch the OIDC client secret server-side without leaking it.
- [x] Basic-auth fallback when OIDC misbehaves — commits `3c9c4b0`, `7289ffb`. Login page renders an "OIDC not working? Sign in with username + password" link when basic creds are configured; `/login?fallback=basic` forces it. Bot's `/api/auth/verify` and dashboard's `/api/auth/login` both accept basic creds whenever a basic user exists, regardless of active mode.
- [x] All OIDC/auth redirects use PUBLIC_URL, not container hostname — commit `7f6fbf9`. Callback success+error, proxy login redirect, logout fallback, oidc-start error all resolved against PUBLIC_URL. Was redirecting users to `https://<docker-id>:3000/` after OIDC login.

#### HTTPS via Caddy + Let's Encrypt (2026-05-01)
- [x] Caddy reverse proxy with Cloudflare DNS-01 plugin (xcaddy build) — commit `53864ae`. New `caddy/` directory with custom Dockerfile and bootstrap Caddyfile. Admin API on `:2019` for dynamic config push.
- [x] Bot endpoints `GET /api/tls/config` (public, no token leak), `POST /api/tls/configure` (API_KEY-required) — commit `53864ae`. Generates Caddy JSON config with DNS-01 challenge using stored CF token; POSTs to admin API on `/load`.
- [x] Self-signed HTTPS as default after deploy — commit `89b6662`. Caddy issues a local-CA cert immediately, browser warns once, user accepts. No window of plain HTTP cookies/credentials.
- [x] `CADDY_HOST` env to bind self-signed cert to actual hostname — commit `36d0ef8`. `:443 { tls internal }` had no SNI context and failed handshake; now uses `{$CADDY_HOST:localhost}`.
- [x] TLS UI: domain auto-normalization (strip `http://`, trailing slash, port) — commit `0f7e892`. Plus frontend validation when toggling enable=true with empty fields, and server-state-driven badge (not local toggle state) — commits `7289ffb`, `5efa413`.
- [x] `build_internal_https_config` requires `subjects` — commit `7e9b2e6`. The internal-CA fallback config had no subjects in the TLS policy; toggling LE off after deploy made TLS handshake fail (ERR_SSL_PROTOCOL_ERROR) and locked users out. Now requires the `domain` arg or falls back to `CADDY_HOST` env.

#### Misc (2026-04-30 → 05-01)
- [x] TradingView chart routes VVV to `COINBASE:VVVUSD` (not on Binance) — commit `12d6a45`.
- [x] Dashboard `/strategies` page now lists all 21 strategies with descriptions — commit `22d7dd7`. Hardcoded at the time (21 was the live count then); made data-driven by PR #152, see the 2026-07-29 entry below. Registry is at 22 as of 2026-09 (see § 2).

#### Reconcile / state-sync hardening (2026-05-03 → 05-04)
- [x] PaperExchange persists `{balance, positions}` to Redis after every fill; `load_state()` runs at startup before reconcile — commit `526d3cc`. Without this, every container restart wiped paper state, then startup-reconcile orphan-closed every DB row, then strategies re-entered duplicately on the next signal. Penguin_volatility paper showed 13 such cascade-entries on 2026-05-01.
- [x] `Strategy.reset_state()` on base + override on all 13 stateful strategies; `repo.reconcile_positions(on_strategy_close=cb)` callback wired to `runner._reset_strategy_state` — commit `526d3cc`. When 5-min runtime reconcile orphan-closes a DB row, strategy `_in_position` is brought into sync. Eliminates "DB closed but strategy thinks it's still in" desync.
- [x] DB cleanup of 28 orphan-closed positions + 28 buy-only trades from before fix — manual SQL on 2026-05-03. Real PnL trades preserved.
- [x] `rsi_momentum` adds `_in_position` flag — commit `494aec8`. Was emitting CLOSE_LONG every tick where exit cond was true, even when flat. Engine ignored each but logged WARNING per tick (~50/hour). Closes Open-Medium item.

#### Host port exposure closed (2026-07-29)
- [x] **Every published port moved to loopback except Caddy's 80/443.** `docker-compose.yml` published `postgres:5432`, `redis:6379` and `dashboard:3000` on `0.0.0.0`, i.e. on the LAN. The bot ports named in the original backlog item were a non-issue — orchestrator-spawned tenant bots have never published host ports (`PortBindings: null`); the § 3 line claiming otherwise was stale and is now corrected. The severe one was **Redis**: `redis:7-alpine` runs with an empty `requirepass`, so any LAN host could read `dashboard:auth:session_secret` — the HMAC key signing session cookies — and forge an admin session outright, plus the OIDC client secret and the CF API token. `dashboard:3000` bypassed both real ingress paths (CF tunnel and Caddy each reach the container over the compose network), so it also sat outside Cloudflare's rate limiting and bot management. Bound to `127.0.0.1` rather than removed, which closes the LAN exposure while keeping `localhost` dev access and SSH-tunnelled DB clients working. Verified no external client was connected to any of the three beforehand.

#### Vault scanner Phase 1 (2026-05-05) — first PR-flow feature
- [x] HyperLiquid vault scanner: daily catalogue poll → coarse pre-filter → per-vault `vaultDetails` fetch → Sharpe/max-DD/multi-period ROI → quality filter → `vault_snapshots` row + `vault_nav_history` append. Telegram fires `vault.qualified` / `vault.disqualified` events on state change with 24h debounce per vault. New `/vaults` dashboard page (sorted by Sharpe) and `vault_picks` HODL signal alongside the others. Owned by the mainnet bot only (single owner; vaults are mainnet-only on HL and the `/vaults` dashboard is pinned to mainnet — moved from testnet 2026-05-13). Quality filter defaults: age ≥ 180d, AUM \$200k–\$20M, ROI 90/180d > 0%, max DD ≤ 25%, Sharpe(180d) > 1.5, manager equity ≥ 5%, fee ≤ 15%. ROI 365d waived for vaults < 365d. — squash-merge `23dd0bf` (PR #1, branch `feat/vault-scanner`). Plan: `docs/plans/vault-scanner.md`. API research: `docs/hyperliquid-vaults-api.md`. 28 new pytest cases (filters / metrics / poller); full suite 133 passed. **First PR-flow feature**: Copilot found 11 issues on first review (catalogue dropouts not disqualified, NAV history not merged into metrics, full-microsecond `snapshot_at` defeating upsert, unguarded casts in `fetch_details`, 48h cutoff hiding everything on missed poll, per-mode duplicate scanning, cooldown bumped on failure, compose `TELEGRAM_EVENTS` overriding .env update, "—d" rendering for null age, follower count under-reports capped vaults, coarse `apr ≤ 0` filter dropping legit qualifiers); all addressed in commit on the branch before merge.

#### Dashboard: trades-page filters + data-driven /strategies (2026-07-29)
- [x] Trades page filters/pagination — was `LIMIT 50`. Strategy filter, date-range picker, and pagination on `/trades`; filter state is URL-driven so views are shareable/bookmarkable. — squash-merge `2c37e79` (PR #150, branch `feat/trades-filters-pagination`).
- [x] Make `/strategies` page data-driven — page no longer carries its hardcoded 21-descriptor array (which had already drifted: `ath_breakout` shipped and traded but was never documented). Strategy prose moved to `bot/hypertrade/strategies/meta/<name>.json` colocated with each module, read via `meta_loader.py`; the bot's `/strategies` endpoint merges metadata with the live registry (live name/symbol/timeframe always win; unreadable meta files are skipped and an undocumented strategy still lists). Coverage test asserts every registered strategy has a meta file. — squash-merge `97ff1d1` (PR #152, branch `refactor/data-driven-strategies`).

#### Telegram noise: transient HL-fetch failures on strategy ticks (2026-08-31)
- [x] **Suppress Telegram noise on transient HL-fetch failures (strategy ticks).** Replaces both failure modes the strategy-tick path had: per-strategy-per-tick `ErrorOccurred` publishing (~22 events/min during the 2026-05-09 HL outage) and the PR-#24 error-type filter's full suppression (which left a multi-hour outage Telegram-silent — the empty-DataFrame signature `fetch_candles` returns after its tenacity retries never even reached the filter). Now both failure signatures (empty candles in `_run_strategy`, transient exceptions in the tick catch-all via the reused `_is_transient_network_error` predicate) feed an outage-window aggregator in `EngineRunner._settle_fetch_outage()`: a window clearing before `FETCH_OUTAGE_ALERT_SECONDS` (default 600) never notifies; a window persisting ≥ threshold emits exactly ONE `ErrorOccurred` (`strategy="candle-fetch"`) summarizing affected strategies; recovery closes the window log-only. Non-transient errors still publish immediately. Companion fix for HODL verdict-recovery noise was `fix/vault-picks-error-event` (PR #98). Tests: `bot/tests/test_engine/test_fetch_outage_alert.py` (10 cases). — squash-merge `1fb9db1` (PR #158, branch `fix/fetch-outage-dedup`), merged 2026-08-31.

#### Correlation grouping, oleg_aryukov vectorization, backtest history page (2026-09)
- [x] **Correlation grouping.** `family` attribute on `Strategy`, resolved via `registry.get_strategy_family()`; `allow_multi_coin=False` now refuses to stack two strategies in the same family (e.g. `cdc_macd`/`macd_zero`, both an EMA12/26-cross ≡ MACD-zero-cross signal) across any coin, not just the same coin. — squash-merge `3b6433a` (PR #157, `feat: strategy family grouping and family-level multi-coin gate`).
- [x] **Optimize `oleg_aryukov` for backtest.** Vectorized the Nadaraya-Watson kernel and per-bar RCI loop (behavior-preserving — live signal output unchanged). — squash-merge `37e490d` (PR #160, `refactor: vectorize oleg_aryukov`).
- [x] **Surface backtest history in dashboard.** New `/backtests` page: filters, trend chart, pager over the `backtest_runs` table. — squash-merge `0be8a2f` (PR #159, `feat(dashboard): /backtests page with filters, trend chart, pager`).

#### Full-stack analysis (2026-09-15)
- [x] Full-stack analysis 2026-09-15 — live production state (operator tenant, all three modes), strategy performance since the 2026-05-29 evaluation, a money-path review of the bot engine, a tenant-isolation review of the dashboard, local quality gates, and documentation drift. Read-only; nothing on the server, in Redis, or in the DB was changed. Surfaced the reconcile read-failure bug (now Open — Critical above), the engine/dashboard hardening items (now Open — Medium above), and this file's drift (fixed by this PR). Report: `bot/reports/analysis-2026-09-15.md` (PR #163).

---

## 6. Working principles

### Investigation before code

Don't fix what you don't understand. The order is always:
1. Reproduce or observe the problem (log line, DB row, dashboard screenshot).
2. Read the relevant code top-to-bottom — no skimming.
3. Write the fix and a check that would have caught it.
4. Deploy. Verify.

If a fix is "obvious" without step 1, the fix is probably wrong.

### Keep DB and exchange in lockstep

The single most dangerous failure mode in this system is DB ↔ exchange divergence (we already lived through it). Any new feature that opens or closes positions must:
- Write to DB **before** sending the order, OR record the order ID and reconcile.
- Tolerate the case where the order succeeds but the DB write fails.
- Be idempotent on retry.

The reconcile function is the safety net, not the strategy. Don't lean on it for correctness.

### Don't add features without a need

We have 22 strategies and a decent UI. Resist the urge to add more strategies, more pages, more abstractions. The backlog is the product roadmap.

### Logs are the API

Every non-trivial action — open, close, skip, reconcile, error — logs a line that includes: strategy name, symbol, side, size, price, and the *reason*. If you can't tell from the logs why something happened, the logging is the bug.

### Telegram is for humans

Don't spam Telegram. Forward `trade.executed`, `position.closed`, and `error`. Skip `signal.generated` (it duplicates `trade.executed`) and any verbose tick-level events.

### Money handling

- All sizes use `position.size` from DB at close time, never recomputed.
- All prices are floats; never compare floats with `==`. Use tolerance windows (`abs(a - b) < 1e-6`).
- Fees are subtracted at trade-record time, not at signal time.
- Leverage applies to notional, not margin. `notional = margin * leverage`. Stop-loss distances are on price, not notional.

### Testing

Every strategy has a unit test in `bot/tests/test_strategies/test_strategies.py` covering: warmup guard, entry signal fires, restore_state doesn't instant-close, SL exit fires. New strategies must add tests in the same shape. Run with `cd bot && uv run pytest`.

The `paper` mode is the integration test — it runs the same code against a simulated exchange. Use it to validate end-to-end behavior before promoting to testnet.

---

## 7. Workflow per task

**As of 2026-05-05, all new features go through a feature branch + PR
flow.** Direct pushes to master are reserved for emergency fixes and
trivial doc tweaks. Hotfixes can also branch (`fix/<short-name>`) when
the change has any risk of regression.

### Standard flow (feature work)

```
1. State the task in one sentence. Update the backlog if new.
2. Investigate (read code, logs, DB rows).
3. Plan — write to docs/plans/<feature>.md for non-trivial work.
   Get user sign-off on the plan before coding.
4. Create branch: git switch -c <type>/<short-name>
   Types: feat | fix | docs | refactor | chore
   Examples: feat/vault-scanner, fix/penguin-restart, docs/api-readme
5. Implement on the branch.
6. Test locally (pytest, type-check, dry-run if possible).
7. Commit with Conventional-Commit-style message + Co-Authored-By line.
   Multiple commits per branch are fine — they get squashed at merge.
8. Push: git push -u origin <branch>
9. Open PR: gh pr create --fill --base master
   Use a HEREDOC body with: ## Summary, ## Test plan, ## Notes for reviewer.
10. Get the PR reviewed before merging — there is NO automated code
    reviewer (see "Review before merge" below). Zero comments means the
    PR is unreviewed, not approved: request a human review or run the
    repo-native `.github/review-team/` reviewer fleet, then triage every
    finding (fix or reply).
11. Merge once review is done and findings addressed:
    gh pr merge --squash --delete-branch
12. Pull master locally; deploy to server with standard command.
13. Verify (logs clean, dashboard correct, parity check).
14. Mark backlog item Done with the merged-PR's squash-commit hash.
```

### Direct-to-master allowed (skip the PR)

Only these:
- README typos, comment fixes, single-word doc edits.
- Reverting a freshly-broken master commit (use `git revert`, not force-push).
- True emergencies where the bot is down and waiting on review costs money.

When unsure: branch + PR. The five extra minutes are negligible vs. the
cost of a regression in master.

### Branch naming

- `feat/<short-name>` — new feature or signal
- `fix/<short-name>` — bug fix
- `docs/<short-name>` — docs-only changes (README, CLAUDE.md, plans)
- `refactor/<short-name>` — internal restructuring, no behavior change
- `chore/<short-name>` — dependency bumps, gitignore, CI

Keep names short but specific: `feat/vault-scanner`, not
`feat/new-vault-thing`. Use kebab-case.

### PR description template

```markdown
## Summary
- 1-3 bullets: what this changes and why

## Test plan
- [ ] pytest passes
- [ ] specific manual checks (e.g. "/hodl page renders new card")
- [ ] deploy verified (or "deploy after merge")

## Notes for reviewer
- Anything subtle, intentional trade-offs, or follow-up work
```

### Commit message style

```
<type>: <imperative summary, <72 chars>

<paragraph explaining WHY — the diff shows what>

Co-Authored-By: Claude <model> <noreply@anthropic.com>
```

`<type>` matches the branch type prefix (feat/fix/docs/refactor/chore).
Multiple commits on a branch don't need to be perfectly clean —
`gh pr merge --squash` collapses them into one with the PR title +
description as the merged commit.

### Review before merge — there is no automated code reviewer

**Measured 2026-08-31** (`gh pr view <N> --json statusCheckRollup` on
recent PRs): the only checks that run on every PR are

- `gitleaks` — secret scan (`.github/workflows/secret-scan.yml`)
- GitHub code scanning, CodeQL default setup (configured in repo
  settings; no workflow file in the repo): `Analyze (actions)`,
  `Analyze (javascript-typescript)`, `Analyze (python)`

Both are security/static scans. **Nothing on GitHub reviews the code.**

Copilot's PR review was active on this repo, then silently stopped:
the last Copilot review landed on PR #134 (2026-05-17), and
every PR since (#135 → #160 as of 2026-08-31) has zero reviews. The
previous "wait ~60s for Copilot, then merge" instructions were removed
2026-08-31 because an agent following them read the absence of Copilot
comments as approval and merged five PRs (#156–#160) with no code
review at all. Absence of review comments is not a signal — it only
means no reviewer has looked.

**Merge rule: a PR with no review is unreviewed, not approved.** Before
`gh pr merge`, one of these must be true:

1. **A human reviewed the diff** — approve, request-changes, or inline
   comments that are all addressed (fix or reply, triage below).
2. **An explicit agent review pass ran and its findings were triaged.**
   The repo ships repo-native reviewer prompts for the `review-team`
   skill in `.github/review-team/` (11 lenses, added in PR #156). Run
   it, classify every finding (real bug / style nit / spurious), fix or
   reply, and record in the PR that the fleet pass happened.
3. **The operator explicitly waived review** for a trivial change —
   the operator's call, same spirit as the direct-to-master exceptions.

If no review is available, **leave the PR open** (the Kanban card stays
in review). Do not merge to unblock. The local gates remain mandatory
but are not sufficient: `pytest` / `vitest` + `build` green proves the
tests pass, not that the code is right.

Reply to an inline review comment (any reviewer):
```
gh api -X POST repos/<owner>/<repo>/pulls/<N>/comments/<comment-id>/replies \
  -f body="..."
```
List comment IDs:
```
gh api repos/<owner>/<repo>/pulls/<N>/comments --jq '.[] | {id, path, line}'
```

`scripts/pr-watch.sh` polls for a Copilot review and is currently inert
on this repo — no Copilot review will arrive, so it just times out. It
is kept only in case Copilot code review is re-enabled; if that
happens, update this section first, then re-arm the script.

### When to ask the user vs. just do it

**Just do it (still requires PR):**
- Code fixes, refactors, new tests.
- Adding logging, retry logic, reconcile improvements.
- Updating docs, `.env.example`, README.
- New HODL signals or features that fit existing architecture.
- Recommending strategy disable based on evidence.

**Ask first (and write a plan in `docs/plans/`):**
- Anything that modifies production DB rows beyond the migration tooling.
- Disabling a strategy in live config (recommend, then ask).
- Sending real funds, mainnet trading.
- `git reset --hard` on shared branches, force-push.
- Changing the architecture in a way that touches >5 files.
- Removing strategies, columns, or endpoints (vs. deprecating).
- New features that don't fit existing architecture (e.g. vault scanner —
  sit between exchange/ and hodl/).

When in doubt, write the plan first and surface it for review.

---

## 8. Strategy evaluation policy

Every Sunday (or on demand), an agent should:

1. Pull the past 7 days of trades per strategy from `mode='testnet'`.
2. Compute per strategy: number of trades, win rate, total realized PnL, average PnL per trade, max consecutive loss.
3. Compare to the strategy's archetype (e.g. mean-reversion strategies should have ~50%+ win rate; momentum follow-throughs should have <40% win rate but bigger wins).
4. Flag for review:
   - Strategies with 0 trades for 14+ days (data feed broken? signal logic dead?).
   - Strategies with realized PnL more than 2σ below their backtest expectation.
   - Strategies that consistently lose to fees+funding (gross-positive but net-negative).
5. `hypertrade.reports.weekly_eval` computes the evaluation and posts it to
   Telegram (Sunday 18:00 digest, or on-demand via `/eval [days]`) and CLI
   stdout — it does not write a file. Full write-ups are hand-written by the
   agent under `bot/reports/` when the finding warrants a durable report
   (e.g. `bot/reports/strategy-eval-2026-05-29.md`,
   `bot/reports/analysis-2026-09-15.md`); there is no automation that
   creates `bot/reports/weekly-YYYY-MM-DD.md`.

The agent **recommends** disable, the user **decides** disable. Disable is done via the dashboard `/options` page or the `/api/control/strategy/{name}/toggle` endpoint, never by editing strategy code.

---

## 9. Common pitfalls — read before debugging

- **`asyncio.to_thread` + `asyncio.run` deadlock with asyncpg in Python 3.13.** This burned us with Alembic. Don't call `asyncio.run` from inside a thread that's already in an event loop. Use `loop.run_in_executor` instead, or run blocking code in a real subprocess.
- **HyperLiquid position is netted per-coin.** If two strategies open opposing sides on the same coin, the exchange shows the net. The DB will diverge unless `allow_multi_coin=False` is enforced (which it now is by default).
- **HyperLiquid sizes have per-asset `szDecimals`.** BTC is 5 decimals, SOL is 2. Round before submitting orders or you get cryptic 422s. The exchange wrapper already does this — don't bypass it.
- **HyperLiquid prices have a 5-significant-figures rule.** Same wrapper handles it.
- **Telegram HTML parse mode breaks on raw `<` and `>`** in dynamic strings. Always `html.escape()` user-supplied or model-supplied text. Already wrapped in `notify/telegram.py`.
- **`docker compose exec -T`** is required for non-TTY commands over SSH. Forgetting `-T` makes the command hang.
- **Postgres `is_open == True` in SQLAlchemy** must be `== True` (not `is True` or just `is_open`). Drizzle and SQLAlchemy both lift `True` to `1` in the SQL but only with explicit comparison.
- **Settings are loaded once at process start.** Changing `.env` requires a container restart. Runtime overrides go through Redis (`BotControl`), not env vars.
- **`req.url` inside Docker returns the container's bound hostname**, not the public hostname users see through the reverse proxy. Any redirect built from `req.url` (NextResponse.redirect, OIDC redirect_uri, login redirects) will send users to `https://<docker-id>:3000/`. Always resolve through `PUBLIC_URL` (or `DASHBOARD_URL` as fallback). Pattern: `lib/oidc.ts:resolveRedirectUri()`, `proxy.ts` login fallback, `app/api/auth/oidc/callback/route.ts:publicLoginUrl()`.
- **Caddy JSON config requires explicit `subjects` in TLS automation policies.** A bare `:443 { tls internal }` Caddyfile block works (Caddy infers from the host directive), but the equivalent JSON config without `subjects` causes TLS handshake to fail (ERR_SSL_PROTOCOL_ERROR) because Caddy doesn't know which SNI to issue for. Same applies to host-match on HTTPS routes — without it any non-matching SNI returns 404.
- **Bitwarden / password managers and React controlled inputs.** Bitwarden injects values via direct DOM property assignment, which doesn't trigger React's `onChange` event. The injected value is silently overwritten on next render. Workaround: use `useRef` + `defaultValue` for password and username inputs (uncontrolled), read at submit time. Required `name` attributes for the manager to identify fields, plus `autoComplete="current-password"` (or `"new-password"` only when truly setting up).
- **Caddy admin API on `:2019` is open inside the Docker network.** Never publish it externally. The bot reaches it via `http://caddy:2019/load` for dynamic config push.
- **Caddy's `local_certs` global directive doesn't help when `subjects` is missing.** It just sets the issuer policy default; subjects still required.
- **OIDC `redirect_uri` mismatch is silent.** If start sends one URL and the provider's allowlist contains another, the user gets a generic auth error from the provider — no log on our side until you bisect. Always verify with `docker logs hypertrade-dashboard-1 | grep oidc` and the provider's audit log.
- **`PUBLIC_URL` must include the scheme.** `$DEPLOY_HOST` won't work — must be `https://$DEPLOY_HOST` (or `http://...` for dev). Used as base for redirect URLs throughout.
- **Caddy `http_only_config` is gone.** The bot's old `build_http_only_config()` is now an alias for `build_internal_https_config()` — there is no plain-HTTP fallback any more by design. If TLS breaks, fix it; don't disable TLS.

---

## 10. Glossary

- **Mode:** one of `paper` / `testnet` / `mainnet`. Selected by `EXCHANGE_MODE` env. Each bot container picks one at startup; the dashboard switches between them via `?mode=` query param.
- **Signal:** a `Signal` dataclass returned by a strategy's `on_candle()`. Has `action` (OPEN_LONG / OPEN_SHORT / CLOSE_LONG / CLOSE_SHORT), symbol, strategy_name, optional reason.
- **Tick:** one iteration of the engine runner loop. Default every 60s. Each tick: fetch candles, ask each enabled strategy `on_candle`, execute resulting signals.
- **Position:** a row in the `positions` table representing one strategy's view of its open exposure. The exchange may show a different (netted) view — see § 9.
- **Trade:** a row in `trades` table representing a single fill (open or close).
- **Equity snapshot:** total account value at a point in time, written every tick.
- **Reconcile:** the operation that compares DB open positions to exchange positions and closes DB orphans. See `Repository.reconcile_positions()`.
- **Allow_multi_coin:** Redis flag. When false (default), only one strategy can hold a position per coin, and at most one strategy per `family` may hold a position across all coins (correlation grouping — e.g. cdc_macd and macd_zero are both the EMA12/26 ≡ MACD-zero-cross signal and never stack). Enforced in `runner._execute_signal()`; families are declared on the strategy classes and resolved via `registry.get_strategy_family()`.
- **Flip-detect:** Engine logic that synthesizes a CLOSE signal when a strategy emits OPEN_X while DB shows the opposite side open for that strategy. Prevents HL netting from leaving a partial-direction position.
- **Export_state / restore_from_json:** Strategy-level methods that serialize internal state (SL, TP, trail, entry) to `positions.state_json` at signal time and read it back verbatim on bot restart. Eliminates SL drift across restarts. Implemented on all 8 stateful strategies.
- **Self-signed default:** When TLS is enabled but Let's Encrypt isn't configured (or fails), Caddy issues a cert from its internal CA for `CADDY_HOST`. Browser warns once, user accepts. Auth cookies never cross the network in cleartext.
- **PUBLIC_URL:** Env var on the dashboard container. The canonical user-facing URL (e.g. `https://$DEPLOY_HOST`). Every redirect that the user's browser follows must be built from this — never from `req.url`.
- **CADDY_HOST:** Env var on the Caddy container. The hostname Caddy issues a self-signed bootstrap cert for. Set to match `PUBLIC_URL`'s host so browsers don't get a name-mismatch warning on top of the self-signed warning.
- **vvv_hedge:** Custom defensive hedge strategy for staked VVV holdings. NOT a Pine port — designed in-house. Mandatory EMA-bearish filter + 2-of-3 supplementary indicators, fixed `holding_vvv` size, hard 10% SL.
- **Backtest CLI:** `cd bot && uv run python -m hypertrade.backtest --strategy <name> --days N`. Auto-saves to `backtest_runs` table; use `--no-save` to opt out. Use `--all` to sweep every registered strategy.

---

This file is the source of truth. When in doubt, update it rather than build
folklore. If a workflow described here is wrong, fix the workflow first and
the code second.
