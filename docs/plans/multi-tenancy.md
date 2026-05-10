# Multi-tenancy — design plan

**Status:** RFC, awaiting review.
**Decided:** 2026-05-10.
**Author:** Claude (briefed by operator).
**Tracking PR:** #35.

This document captures the architecture for turning hypertrade from a
single-operator personal bot into a multi-tenant deployment where each
authenticated user runs their own isolated bot with their own keys,
their own strategies, their own positions.

The current operator's deployment becomes "tenant 1" with no user-facing
disruption (zero-downtime cutover described in § Backwards compat).

---

## 1. Decisions (locked)

These five decisions drive everything below. Re-litigating them means
redesigning sections 3-7.

| # | Question | Decision |
|---|---|---|
| 1 | Trust model | **B — user-encrypted secrets** via passphrase; operator (host owner) cannot read user secrets even with DB access |
| 2 | Bot process model | **Per-user container, ONE mode per user** (paper OR testnet OR mainnet, not all three). User picks mode at bot create time; switch requires container recreate |
| 3 | Authentication | **Federation only (Authentik OIDC)** — no local username/password, no email/password registration |
| 4 | Telegram | **Webhook** (each user-bot registers a unique webhook URL, no polling) |
| 5 | Registration | **Invite-only** via Authentik group membership (`hypertrade-users` group); new SECURITY.md needed |

---

## 2. Updated threat model (replaces SECURITY.md "single tenant" framing)

Current threat model: "operator runs unmodified master with their own
config; everything inside the deploy is implicitly trusted."

Multi-tenant model:

- Each user is an opaque `tenant_id` (uuid). Tenants are mutually
  untrusting peers from the data-layer's perspective.
- Operator (host owner) is a privileged role for ops (deploy, scale,
  backup) but **MUST NOT** be able to read tenant secrets (keys,
  Telegram tokens, etc) at rest. Achieved via passphrase-derived
  encryption (§ 4).
- Authentik gates entry — only users in the `hypertrade-users` group
  can sign in. Operator manages group membership; this is the only
  registration mechanism.
- Tenant boundary bypass (A reads B's data, places orders on B's HL
  account) is **CRITICAL** — same severity as RCE. Multiple defense
  layers: row-level security, mode-namespaced Redis keys, container
  isolation.

### New in-scope vulnerability classes (vs current SECURITY.md)

- Tenant cross-read (A queries B's positions / trades / events)
- Tenant cross-write (A's API call routes to B's container)
- Tenant cross-spend (A's HL key signs B's orders)
- Privilege escalation from tenant role → operator role
- Passphrase brute force / weak-passphrase attacks against stored
  secret blobs
- Resource starvation by single tenant (CPU, HL rate limits, container
  sprawl)
- Authentik compromise = full breach (single SSO point of trust)
- Webhook URL leakage = ability to forge Telegram updates for a tenant

### Out of scope (still)

- Operator with root + memory access reading runtime decrypted secrets
  from a running container. Defending against root-on-the-host is
  out of any practical threat model for a self-hosted Python app.
- Authentik bugs, Phase bugs, HL bugs (we trust upstream).

---

## 3. Architecture overview

```
┌───────────────────────────────────────────────────────────────────┐
│ Caddy (one process, dynamic config)                                │
│   :443 → routes by Host + path                                     │
│     /api/auth/*               → dashboard                          │
│     /api/tenant/<id>/*        → dashboard (tenant API proxy)       │
│     /api/telegram/<id>        → dashboard (Telegram webhook in)    │
│     /                         → dashboard (Next.js)                │
└────────────────┬──────────────────────────────────────────────────┘
                 │
┌────────────────▼──────────────────────────────────────────────────┐
│ Dashboard (Next.js)                                                │
│   - OIDC auth via Authentik (existing flow, kept)                  │
│   - Per-tenant settings UI: passphrase, secrets entry, bot start   │
│   - Encryption helpers (Argon2id KDF, AES-GCM)                     │
│   - Bot lifecycle controller (Docker API or systemd unit per user) │
│   - Tenant-scoped REST endpoints (/api/tenant/<id>/*)              │
│   - Telegram webhook receiver, routes to bot via Redis pub/sub     │
└────┬──────────────────────────────────────────────────────────────┘
     │
     │ Docker API (UNIX socket bind-mount)
     │
┌────▼──────────────────────────────────────────────────────────────┐
│ Per-user containers — N parallel, one per user                     │
│   hypertrade-bot-tenant-<id>                                       │
│     env: TENANT_ID=<uuid>, EXCHANGE_MODE=<paper|testnet|mainnet>,  │
│          (decrypted secrets injected at start time only)           │
│     restart: unless-stopped                                        │
│     resource limits: CPUs=1, mem=512Mi (configurable)              │
└────┬──────────────────────────────────────────────────────────────┘
     │
┌────▼──────────────────────────────────────────────────────────────┐
│ Shared infra                                                       │
│   Postgres (RLS by tenant_id) | Redis (key prefix tenant:<id>:)    │
└────────────────────────────────────────────────────────────────────┘
```

---

## 4. Secret encryption (decision B)

### Encryption flow at rest

1. User signs in via Authentik → server has session keyed by Authentik
   `sub` claim
2. User enters passphrase ONCE in dashboard settings (separate from
   Authentik password). Passphrase is sent over TLS.
3. Server runs Argon2id (memory=64MB, iterations=3, parallelism=4) over
   `passphrase + per-tenant random salt` → 256-bit key K
4. Server stores: `salt`, `verification_token = HMAC(K, "verify")` in DB
   (`tenants.passphrase_salt`, `tenants.passphrase_verifier`)
5. Subsequent logins: user re-enters passphrase → server derives K →
   compares verifier → grants in-memory cache of K
6. Each secret is encrypted as `AES-GCM(K, secret_plaintext, random_nonce)`
   and stored alongside its `nonce` and `ciphertext`. K is NEVER
   persisted.

### Bot-runtime injection

When a tenant starts their bot:
- Dashboard requires the user's passphrase active in session
- Dashboard derives K, decrypts each secret, injects as env var into
  the new container at `docker run` time
- Container runs with plaintext secrets in env (standard practice; same
  as today's deploy)
- After container start, K may be flushed from server memory — bot has
  what it needs in its own process

### Bot-restart implications

- Container `restart: unless-stopped` means OS will restart on crash
- BUT: a clean restart preserves env vars in the container's config.
  An OOM-kill or `docker restart` keeps them.
- If user **manually stops + starts** the bot, they re-enter passphrase
- If **operator restarts the host** (full reboot, planned downtime),
  bots come back UP because env was persisted in container config —
  but secrets are still on disk under `/var/lib/docker`. **This is a
  trade-off**: convenience vs operator-can-still-read-running-config.
- **Recovery story**: lost passphrase = secrets unrecoverable. User
  must re-enter every secret + set new passphrase. No reset, no email
  link, no operator override (that's the entire point of B).

### Open question — passphrase caching duration

Three options for how long the derived K stays accessible server-side:

- **Per-request**: user provides passphrase on every protected action.
  Maximum security, terrible UX.
- **Session lifetime**: K cached in encrypted session cookie or Redis
  with same TTL as Authentik session (default ~1d). User logs in →
  enters passphrase once → can manage bot for that day.
- **Long-lived "unlock"**: user opts to cache K for N hours/days/weeks.
  More usable, less secure.

**Default in v1**: session lifetime. Add long-lived unlock as opt-in
later.

---

## 5. Database schema changes

### New table: `tenants`

```sql
CREATE TABLE tenants (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  authentik_sub         TEXT UNIQUE NOT NULL,    -- OIDC sub claim
  email                 TEXT NOT NULL,
  display_name          TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  passphrase_salt       BYTEA,                   -- 16 bytes
  passphrase_verifier   BYTEA,                   -- 32 bytes (HMAC of derived K)
  bot_mode              TEXT,                    -- 'paper' | 'testnet' | 'mainnet' | NULL (no bot yet)
  bot_container_id      TEXT,                    -- docker container id when running
  is_active             BOOLEAN NOT NULL DEFAULT true,
  last_login_at         TIMESTAMPTZ
);
```

### New table: `tenant_secrets`

```sql
CREATE TABLE tenant_secrets (
  tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key                   TEXT NOT NULL,           -- e.g. 'HYPERLIQUID_PRIVATE_KEY'
  ciphertext            BYTEA NOT NULL,
  nonce                 BYTEA NOT NULL,          -- 12 bytes for AES-GCM
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (tenant_id, key)
);
```

### Existing tables: add `tenant_id`

```sql
ALTER TABLE positions          ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE trades             ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE equity_snapshots   ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE strategy_configs   ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE funding_payments   ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE backtest_runs      ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE vault_snapshots    ADD COLUMN tenant_id UUID REFERENCES tenants(id);
ALTER TABLE vault_nav_history  ADD COLUMN tenant_id UUID REFERENCES tenants(id);

-- Index every tenant_id column
CREATE INDEX idx_<table>_tenant ON <table>(tenant_id);

-- RLS policies (tenant can only see own rows; operator role bypasses)
ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON positions
  USING (tenant_id = current_setting('app.tenant_id')::UUID);
-- ... repeat per table
```

### Backfill (current operator → tenant 1)

```sql
INSERT INTO tenants (id, authentik_sub, email, display_name, bot_mode)
  VALUES (
    '00000000-0000-0000-0000-000000000001',
    -- operator's Authentik sub from existing OIDC config
    (SELECT auth_oidc_sub FROM legacy_config),
    'operator@example',
    'Operator',
    'mainnet'
  );

UPDATE positions          SET tenant_id = '...0001' WHERE tenant_id IS NULL;
UPDATE trades             SET tenant_id = '...0001' WHERE tenant_id IS NULL;
-- ... per table

ALTER TABLE positions          ALTER COLUMN tenant_id SET NOT NULL;
-- ... per table after backfill
```

### Redis namespacing

All keys today: `hypertrade:{mode}:{...}`.
New: `hypertrade:tenant:{tenant_id}:{mode}:{...}`.

`BotControl.__init__` takes `tenant_id` arg, prefixes accordingly.
Backwards compat for tenant 1: same key shape via the migration.

---

## 6. Per-user container orchestration

### Container lifecycle

User flow in dashboard:
1. Sign in via Authentik (existing) → tenant row created or fetched
2. Settings page: "Set passphrase" (first time) + "Add secrets" (HL key,
   Telegram token, etc — see § 7)
3. "Create bot" form: pick `EXCHANGE_MODE` (paper/testnet/mainnet) →
   server validates required secrets present for that mode, decrypts
   secrets with K, calls Docker API to start container
4. Bot status page: running/stopped/crashed, recent logs, P&L summary

### Container naming + isolation

- Name: `hypertrade-bot-tenant-<short_id>` (first 8 chars of UUID)
- Network: shared internal docker network (so it can reach
  postgres/redis), no public ports
- Resource limits: 1 CPU, 512MB RAM by default; configurable per-tenant
  by operator
- Bind mount: none (no shared volumes)
- Env vars: `TENANT_ID`, `EXCHANGE_MODE`, plus all decrypted secrets
- Image: same `hypertrade-bot` image as today (single binary that reads
  `TENANT_ID` to scope its DB queries + Redis keys)

### Switching mode

ONE bot per user, ONE mode per bot. To switch from paper to testnet:
1. User stops their bot
2. Updates `tenants.bot_mode`
3. Restarts → new container, new mode

(No "all three modes simultaneously" like today's operator setup. The
operator's existing 3-mode setup stays as-is during migration; only
NEW tenants get one-mode-only.)

### Operator API for bot lifecycle

New dashboard endpoints:
- `POST /api/tenant/me/bot/start` (body: `{passphrase, mode}`) — derives
  K, decrypts secrets, starts container
- `POST /api/tenant/me/bot/stop` — stops container, removes it
- `POST /api/tenant/me/bot/restart` (requires passphrase again)
- `GET  /api/tenant/me/bot/status` — running, mode, uptime, equity

All gated by Authentik session + tenant ownership check.

---

## 7. Settings UI

### Sections needed

1. **Account** — Authentik profile (read-only), tenant ID, "delete my
   account" (destructive, asks for passphrase)
2. **Passphrase** — set initial / change (change requires re-entering
   old + entering new + re-encrypting all stored secrets)
3. **Secrets** — table of stored secret keys with "set/update/delete"
   actions. Per secret:
   - HyperLiquid private key (mainnet/testnet, depending on bot mode)
   - HyperLiquid account address (for API-wallet pattern)
   - Telegram bot token
   - Telegram chat ID
   - API key for the bot's HTTP API (auto-generated on bot create)
4. **Bot** — pick mode, see status, start/stop/restart
5. **Strategies** — enabled set per tenant (defaults to none on
   mainnet, all on paper/testnet — same as audit C3 logic)

### Validation hooks

Before saving an HL key:
- Decrypt-roundtrip test (encrypt with K, decrypt with K, compare)
- Optional: derive address from key, ping HL `/info` for that address
  to confirm it's a valid wallet

---

## 8. Telegram webhook routing

### Pre-fix today

`TelegramNotifier` long-polls per bot via `getUpdates`. One bot = one
poll loop. N tenants = N concurrent polls. Bad scaling.

### Multi-tenant webhook design

1. When tenant sets up Telegram, dashboard calls
   `setWebhook(url=https://<host>/api/telegram/<tenant_id>, secret_token=<hex>)`
2. Telegram POSTs all updates for that bot to that URL
3. Dashboard receives, validates `X-Telegram-Bot-Api-Secret-Token`
   header against stored secret_token
4. Dashboard publishes the update to Redis channel
   `hypertrade:tenant:<id>:telegram:in`
5. Bot container subscribes to that channel, processes update locally,
   replies to Telegram via the Bot API (using its decrypted token)

### Why this routing (vs direct webhook to bot container)

- Bots are on internal docker network, not exposed
- One Caddy entry point handles TLS for all
- Dashboard handles auth (`secret_token` validation), bot container
  trusts the channel
- Allows future "push notifications" features without exposing N URLs

### Caddy config

Already dynamic-able via the admin API (we use it for TLS today). Add
a single catch-all route `/api/telegram/<tenant_id>` → dashboard.

---

## 9. Phase rollout (target: 6-8 weeks part-time)

Each phase = one PR, mergeable to master incrementally. Tenant 1
(operator) keeps working through the whole rollout — no downtime.

| # | Phase | Key changes | Roughly |
|---|---|---|---|
| 1 | DB schema | Add `tenants`, `tenant_secrets` tables; add `tenant_id` to existing tables; backfill operator as tenant 1; alembic migration | 1 week |
| 2 | Auth + settings UI shell | Tenants page, "set passphrase" flow, Argon2id + AES-GCM helpers, secret CRUD endpoints (no bot yet) | 1 week |
| 3 | Per-user container lifecycle | Docker API integration, bot start/stop/status endpoints, image stays same but reads `TENANT_ID` to scope queries | 1-2 weeks |
| 4 | Telegram webhook routing | Webhook receiver, Caddy route, Redis pub/sub, bot reads from channel | 1 week |
| 5 | Tenant isolation hardening | RLS policies on every table, integration tests for cross-tenant access attempts, audit logging | 1 week |
| 6 | Cutover for operator | Migrate current 3 bots to new model (operator runs as tenant 1 with one bot per mode? or pick one mode?), decommission `*-bot-paper`/`-testnet`/`-mainnet` containers | 1 week |
| 7 | SECURITY.md rewrite + invite-onboarding docs | New threat model, Authentik group setup guide, README update | 0.5 week |
| 8 | Closed-beta with 1-2 invited users | Real-world stress test, fix discovered issues | 1+ week |

---

## 10. Backwards compatibility

The current operator's deployment continues to work throughout the
migration:

- **Phase 1**: alembic migration adds tenant_id columns + backfills
  current operator as tenant_id=`...0001`. Bot code still works because
  Repository's queries can either ignore tenant_id (legacy mode) or
  filter on it (new mode). We feature-flag this.
- **Phase 2-3**: dashboard gains new pages but the operator still uses
  the old single-mode UI. New pages are gated to "tenant != 1" or
  via feature flag.
- **Phase 4**: Telegram polling still works for tenant 1; new tenants
  use webhook. Both modes coexist.
- **Phase 5**: RLS policies grant operator role full access (the
  `app.is_operator=true` setting); new tenants use scoped access.
- **Phase 6**: operator's 3-mode deploy gets re-architected. Two
  options: (a) keep all 3 as "system bots" outside tenant model; (b)
  operator becomes a normal tenant + picks ONE mode like everyone
  else. Pre-decision: option (a) — current setup stays, multi-tenant
  is additive.

Migration rollback: every alembic migration is reversible. No
destructive backfill — `tenant_id` columns are nullable until phase 5.

---

## 11. Open questions

1. **Passphrase recovery**: zero (user loses passphrase = secrets gone).
   Confirmed acceptable per decision B. UX should be very explicit
   ("YOU CANNOT RECOVER THIS — write it down").
2. **Operator visibility into tenant bots**: can operator stop a
   misbehaving tenant's bot (e.g. spam-trading)? **Proposed**: yes,
   operator role can stop any bot and disable the tenant, but cannot
   read secrets.
3. **Resource quotas**: how many tenants per host? **Proposed**: hard
   cap configurable in env (`MAX_TENANTS=10`), with per-tenant 1 CPU
   + 512MB defaults. Dashboard refuses bot create when at cap.
4. **Bot-mode switch**: changing mode = full restart (acceptable). DO
   we let users change mode while bot has open positions? **Proposed**:
   no — mode switch requires no open positions in DB. Force user to
   `/flat` first.
5. **Audit log**: per-tenant audit trail (who changed which secret
   when, who started which bot)? **Proposed**: phase 5 deliverable,
   stored in new `tenant_audit_log` table.
6. **HL rate limits**: HL has per-IP and per-account rate limits. N
   tenants on one IP could starve each other. **Proposed**: monitor
   in phase 8 beta; add per-tenant request bucketing only if it bites.
7. **Backups**: tenant data is encrypted-at-rest with their passphrase.
   Backups of postgres are useless without each user's passphrase.
   Acceptable per decision B; document explicitly.
8. **GDPR / data deletion**: "delete my account" must wipe all
   tenant_id rows + the tenants row + the tenant_secrets row. Cascade
   delete handles it, but logs/backups need policy too.

---

## 12. SECURITY.md rewrite (Phase 7)

The current SECURITY.md is built around "single-operator self-hosted".
Phase 7 rewrites it for the multi-tenant model:

- Threat model: multi-tenant, operator-untrusted-with-secrets,
  Authentik-trusted-for-auth
- In-scope: tenant boundary bypass, secret-at-rest leakage, webhook
  forgery, resource starvation, Authentik account takeover (out of
  our control but document mitigations)
- Out-of-scope: operator with root + memory access, Authentik
  internal bugs, individual user passphrase compromise (their problem)
- Reporting: same GitHub PVR flow; new severity bar for tenant-isolation
  bugs (CRITICAL = same as RCE)
- Response timeline: bumped to "best-effort within 7 days for
  CRITICAL" since it's still a hobby project

---

## Approval

Once this PR merges, we can branch implementation work per phase. Each
phase ships as its own PR with the auto-cycle workflow (Copilot review
→ fix → merge → deploy). Phase 1 is a pure migration — low risk, no
behavior change. Phase 6 is the heaviest — full operator cutover.

Open for review/iteration. Markdown comments, suggestions, "no, that's
wrong" all welcome on the PR.
