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
| 2 | Bot process model | **Per-user container.** Default: ONE bot per user (one mode at a time, switch = container recreate). Multi-bot mode (multiple parallel bots, one per mode) is built into the model and gated by a per-tenant `multi_bot_enabled` feature flag. v1: only the operator (tenant 1) gets the flag set; other tenants see single-bot UI |
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
│     /api/tenant/me/*          → dashboard (own-tenant API; auth    │
│                                  derives tenant_id from session)   │
│     /api/admin/tenants/<id>/* → dashboard (operator-only admin)    │
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
│   - Tenant-scoped REST endpoints (/api/tenant/me/*)                │
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

### What trust model B actually protects (and what it doesn't)

**B protects: secrets in the Postgres `tenant_secrets` table are
unreadable to anyone without the tenant's passphrase.** That's the
guarantee. An operator with full DB access (`SELECT * FROM
tenant_secrets`) gets only ciphertext + nonce; they cannot derive K
without knowing the passphrase.

**B does NOT protect**, in v1:

- **Running container env vars.** Once a tenant unlocks and starts
  their bot, the decrypted secrets are in the container's env (visible
  via `docker inspect` to anyone with Docker socket access — i.e. the
  operator on the host).
- **Container config on disk.** Docker persists container config under
  `/var/lib/docker/containers/<id>/config.v2.json`. Operator with
  root can `cat` this and read env vars of any (running or stopped)
  container.
- **Host root.** The operator who runs `sudo` on the host can attach
  to any process, read its memory, and extract decrypted secrets.

**v1 honest framing**: trust model B protects against an operator who
peeks at the database (or a backup of it) but does NOT protect against
an operator who actively wants to read running-bot secrets. If the
threat model includes "I don't trust the operator at all", this v1
isn't enough — but realistically, anyone running their own
crypto-trading bot already trusts the host they deploy on.

**Future v2 hardening (deferred, not in v1):**

- Inject secrets via `docker exec` to a tmpfs mount inside the
  container, then unlink the file. Bot reads secrets at startup from
  the file path; nothing persists.
- Or use Docker swarm `secrets` (mounted at `/run/secrets/`, tmpfs by
  default).
- Either approach makes "host reboot loses all secrets, must re-unlock
  every bot" the cost. Acceptable trade-off for v2.

### Bot-restart implications (v1)

- Container `restart: unless-stopped` means OS will restart on crash
- A clean restart preserves env vars in the container's config (incl.
  decrypted secrets). OOM-kill, `docker restart`, host reboot — all
  keep the bot running with its existing secrets.
- User-initiated `stop + start` requires passphrase re-entry (because
  we delete the container on stop, not just stop it).
- **Recovery story**: lost passphrase = secrets unrecoverable. User
  must re-enter every secret + set new passphrase. No reset, no email
  link, no operator override (that's the entire point of B's at-rest
  protection).

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
  is_active             BOOLEAN NOT NULL DEFAULT true,
  is_operator           BOOLEAN NOT NULL DEFAULT false,
  multi_bot_enabled     BOOLEAN NOT NULL DEFAULT false,
  last_login_at         TIMESTAMPTZ
);
```

### New table: `tenant_bots`

A tenant may have 1..N bots. Single-bot tenants (the default) have at
most 1 row here; multi-bot tenants (the operator) have up to 3 (one
per mode). The DB-level `UNIQUE (tenant_id, mode)` prevents starting
two bots with the same mode for the same tenant.

The application-level enforcement of "single-bot tenants get exactly 1
bot" is handled in the bot-create endpoint, gated on
`tenants.multi_bot_enabled`.

```sql
CREATE TABLE tenant_bots (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  mode            TEXT NOT NULL CHECK (mode IN ('paper','testnet','mainnet')),
  container_id    TEXT,                          -- docker container id when running
  container_name  TEXT,                          -- e.g. hypertrade-bot-<short_id>-mainnet
  is_running      BOOLEAN NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_started_at TIMESTAMPTZ,
  last_stopped_at TIMESTAMPTZ,
  UNIQUE (tenant_id, mode)
);

CREATE INDEX idx_tenant_bots_running ON tenant_bots(is_running) WHERE is_running = true;
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

-- RLS policies — see § 5.1 below for why we use distinct DB roles
-- per tenant rather than a session-variable approach.
```

### Backfill (current operator → tenant 1)

```sql
INSERT INTO tenants (
    id, authentik_sub, email, display_name,
    is_operator, multi_bot_enabled
)
  VALUES (
    '00000000-0000-0000-0000-000000000001',
    -- operator's Authentik sub from existing OIDC config
    (SELECT auth_oidc_sub FROM legacy_config),
    'operator@example',
    'Operator',
    true,           -- is_operator
    true            -- multi_bot_enabled — only the operator gets this v1
  );

-- Migrate the operator's three existing bots into tenant_bots
INSERT INTO tenant_bots (tenant_id, mode, container_name, is_running)
  VALUES
    ('00000000-0000-0000-0000-000000000001', 'paper',   'hypertrade-bot-paper',   true),
    ('00000000-0000-0000-0000-000000000001', 'testnet', 'hypertrade-bot-testnet', true),
    ('00000000-0000-0000-0000-000000000001', 'mainnet', 'hypertrade-bot-mainnet', true);

UPDATE positions          SET tenant_id = '...0001' WHERE tenant_id IS NULL;
UPDATE trades             SET tenant_id = '...0001' WHERE tenant_id IS NULL;
-- ... per table

ALTER TABLE positions          ALTER COLUMN tenant_id SET NOT NULL;
-- ... per table after backfill
```

### 5.1 RLS via distinct DB roles per tenant (PR #35 review fix)

**The naive approach is broken.** A common pattern is:

```sql
CREATE POLICY tenant_isolation ON positions
  USING (tenant_id = current_setting('app.tenant_id')::UUID);
```

Then the bot does `SET app.tenant_id = '...'` at connection start.
Problem: `current_setting()` is **client-controlled** — a malicious or
compromised tenant bot can simply `SET app.tenant_id = '<other>'` and
read another tenant's rows. No protection.

**v1 design**: every tenant gets a distinct Postgres role at bot
create time. RLS uses `current_user` (which is enforced by the auth
layer, NOT settable by the client).

```sql
-- At tenant_bot create (issued by dashboard's operator-role connection):
CREATE ROLE "tenant_3a2f1e4c" LOGIN PASSWORD '<random_32_bytes>';
GRANT tenant_role TO "tenant_3a2f1e4c";
GRANT SELECT, INSERT, UPDATE, DELETE
  ON positions, trades, equity_snapshots, strategy_configs,
     funding_payments, backtest_runs, vault_snapshots, vault_nav_history
  TO tenant_role;

-- The role-name encodes the tenant_id (first 8 hex of UUID).
-- A canonical mapping function reads the tenant_id from the role name:
CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS UUID AS $$
  SELECT id FROM tenants
   WHERE substring(replace(id::text, '-', '') from 1 for 8)
       = substring(current_user from 8)  -- 'tenant_<8hex>'
$$ LANGUAGE SQL STABLE SECURITY DEFINER;

ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON positions
  USING (tenant_id = app_tenant_id());
-- ... repeat per table
```

The bot connects with its tenant-specific connection string. The
role-password is generated at bot create, stored as a tenant secret
(encrypted with K, like everything else), and injected into the
container env at start.

**Operator role**: separate `operator_role` granted full access via
RLS bypass policy (`USING (true)` for that role). Dashboard connects
as operator role for cross-tenant queries (admin views, reconcile).

**Drop role on tenant delete**: cascades cleanly via `DELETE FROM
tenants` → trigger or app-level orchestration revokes + drops the
role.

**Per-tenant connection pooling**: each bot has its own pool. With
`MAX_TENANTS=10` and 5 connections per bot = 50 max DB connections.
Manageable. If we need to scale further, switch to a connection pooler
(pgBouncer) keyed on the tenant role.

### Redis namespacing

All keys today: `hypertrade:{mode}:{...}`.
New: `hypertrade:tenant:{tenant_id}:{bot_id}:{...}`.

(Note `bot_id` not `mode`: a multi-bot tenant may have e.g. paper +
mainnet running concurrently; bot_id is the unique key.)

`BotControl.__init__` takes `tenant_id` + `bot_id` args, prefixes
accordingly. Backwards compat for tenant 1's three system bots: the
migration grandfather-clauses them to use today's key shape so no
data migration on Redis is needed.

> Redis isolation note: Redis itself has no per-key access control;
> the prefix is application-discipline, not enforced. Tenant bots
> CAN technically read each other's keys if they discover the prefix
> pattern. Mitigation: containers run as non-root, Redis bind only on
> the internal docker network (no external port). For v2 we can
> consider Redis ACL per-tenant credentials.

---

## 6. Per-user container orchestration

### Container lifecycle

User flow in dashboard:
1. Sign in via Authentik (existing) → tenant row created or fetched
2. Settings page: "Set passphrase" (first time) + "Add secrets" (HL key,
   Telegram token, etc — see § 7)
3. "Bots" page:
   - **Single-bot tenant** (default): one card with mode picker + start;
     can stop/start to switch mode
   - **Multi-bot tenant** (operator): list of bot cards, one per mode,
     each with own start/stop/status; "Add bot" button while
     `len(tenant_bots) < 3`
4. Each bot card: status (running/stopped/crashed), recent logs, P&L
   summary

### Container naming + isolation

- Name: `hypertrade-bot-<short_id>-<mode>`
  - Single-bot tenant on testnet: `hypertrade-bot-3a2f1e4c-testnet`
  - Multi-bot operator: `hypertrade-bot-00000001-paper`,
    `hypertrade-bot-00000001-testnet`, `hypertrade-bot-00000001-mainnet`
- Network: shared internal docker network (so it can reach
  postgres/redis), no public ports
- Resource limits: 1 CPU, 512MB RAM per BOT (so a 3-bot operator
  consumes 3 CPU + 1.5GB by default); configurable per-tenant by
  operator
- Bind mount: none (no shared volumes)
- Env vars: `TENANT_ID`, `EXCHANGE_MODE`, `BOT_ID` (the
  `tenant_bots.id` UUID — used in Redis key scoping so two bots from
  the same tenant don't share state), plus decrypted secrets relevant
  to that mode
- Image: same `hypertrade-bot` image (single binary; reads `TENANT_ID`
  + `BOT_ID` to scope all DB queries + Redis keys)

### Bot create flow + multi-bot gate

```python
# Pseudocode: POST /api/tenant/me/bots (create new bot)
def create_bot(tenant, mode, passphrase):
    if not tenant.passphrase_verified(passphrase):
        return 401

    existing = count(TenantBot.where(tenant_id=tenant.id))
    if existing >= 1 and not tenant.multi_bot_enabled:
        return 409, "Single-bot tenant already has a bot — stop the existing one first or contact operator to enable multi-bot mode"
    if existing >= 3:
        return 409, "Maximum 3 bots per tenant (paper/testnet/mainnet)"

    if TenantBot.exists(tenant_id=tenant.id, mode=mode):
        return 409, f"Bot for mode={mode} already exists for this tenant"

    # Validate required secrets present for this mode
    required = required_secrets_for_mode(mode)  # ['HYPERLIQUID_PRIVATE_KEY', ...]
    for k in required:
        if not TenantSecret.exists(tenant_id=tenant.id, key=k):
            return 422, f"Missing required secret: {k}"

    bot = TenantBot.create(tenant_id=tenant.id, mode=mode)
    return 201, bot
```

### Switching mode (single-bot tenant)

To switch from paper to testnet:
1. User stops their existing bot (`POST .../bots/<id>/stop`)
2. Deletes the bot row (`DELETE .../bots/<id>`) — frees the
   (tenant, mode) UNIQUE slot
3. Creates a new bot with the desired mode

(For multi-bot tenants this isn't needed — they just create a second bot
in the new mode while the first runs.)

### Operator API for bot lifecycle

New dashboard endpoints, all gated by Authentik session + tenant
ownership check:

- `GET    /api/tenant/me/bots` — list of own bots
- `POST   /api/tenant/me/bots` (body: `{mode, passphrase}`) — derives K,
  validates required secrets, decrypts and injects, starts container
- `POST   /api/tenant/me/bots/<bot_id>/stop` — stops container
- `POST   /api/tenant/me/bots/<bot_id>/start` (body: `{passphrase}`) —
  re-derives K, re-decrypts, starts again
- `DELETE /api/tenant/me/bots/<bot_id>` — stops + removes container
  + deletes row (frees the mode slot)
- `GET    /api/tenant/me/bots/<bot_id>/status` — running, mode,
  uptime, equity, recent activity

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

### Where is `secret_token` stored? (PR #35 review fix)

Trust model B encrypts the **bot token** (the secret an attacker could
use to impersonate the bot to Telegram). The webhook `secret_token` is
a separate, lower-sensitivity value: it only proves that an incoming
HTTP request came from Telegram and not a random scanner.

Therefore:

- **Bot token**: full passphrase-encrypted, stored in `tenant_secrets`,
  available only when the tenant is unlocked
- **Webhook `secret_token`**: stored as plaintext in `tenant_bots`
  (new column `telegram_webhook_secret`). Always available to the
  dashboard so incoming webhooks can be validated even if the tenant
  isn't currently unlocked.

**Threat if `secret_token` leaks:** an attacker can POST forged
"updates" to `/api/telegram/<tenant_id>` and our dashboard will accept
them and forward to the tenant bot via Redis. The tenant bot then
processes the "update" — but every command handler already gates on
the configured chat_id (existing pattern in `notify/telegram.py`), so
the attacker can only send "updates" purportedly from the configured
chat. To impersonate the chat owner, the attacker also needs the
chat's secrets.

**Net effect of leakage**: attacker can flood our bot with bogus
updates (DoS-ish, annoying). Not financial. Acceptable in the v1
threat model. We log + rate-limit incoming webhook traffic per tenant.

If we ever need stronger protection, encrypt `secret_token` with a
**server-managed key** (separate from user passphrase, lives in
`PHASE`-managed env). Then dashboard can always decrypt without user
unlock, but DB-only attackers still can't read the token.

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
| 6 | Cutover for operator | Operator becomes tenant 1 with `multi_bot_enabled=true`. Their existing 3 mode-named containers are renamed to `hypertrade-bot-<short_id>-<mode>` and registered as 3 rows in `tenant_bots`. The dashboard's "Bots" page shows them as a multi-bot tenant going forward. Operator now uses `/api/tenant/me/bots/*` endpoints like everyone else (no special-case code paths) | 1 week |
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
- **Phase 6**: operator's 3-mode deploy gets re-architected as the
  first multi-bot tenant. Operator's 3 containers are renamed and
  registered as 3 rows in `tenant_bots`; `multi_bot_enabled=true` is
  set on the operator's tenant row. The operator now uses the same
  bot lifecycle endpoints as every other user — just sees 3 cards
  instead of 1. No special-case code paths in the dashboard.
  (Decision change from earlier draft: operator IS a normal tenant,
  with multi_bot_enabled as the only flag that distinguishes them.)

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
4. **Bot-mode switch / bot deletion**: changing mode = stop existing
   bot + delete its `tenant_bots` row + create new one (since UNIQUE
   on `(tenant_id, mode)` ties the slot). Do we let users delete a bot
   while it has open positions in DB? **Proposed**: no — bot delete
   requires no open positions. Force user to `/flat` first. (For
   multi-bot tenants this question rarely comes up since they keep
   each mode running indefinitely.)
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
