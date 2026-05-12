# Phase 6c — tenant-aware dashboard routes + tenant_id backfill

## Why this exists

Multi-tenancy phases 1–7 built the foundation:
- `tenants`, `tenant_bots`, `tenant_secrets`, `tenant_audit_log` schema
- Argon2id passphrase + AES-256-GCM secrets crypto
- Per-tenant container orchestration via dockerode
- Postgres RLS + per-tenant DB roles (`tenant_<32hex>`)
- Operator backfilled as tenant 1 with their existing 3 bot containers

But **none of the dashboard's data routes filter by tenant**. They proxy
straight to operator's `bot-paper`, `bot-testnet`, `bot-mainnet` containers
and SELECT from data tables without a `tenant_id` filter. So any signed-in
user (operator OR beta tenant) sees operator's bot data.

Smoke-tested 2026-05-11 with `betauser1@example.com`: logged in via OIDC,
landed on operator's overview / trades / positions. No `betauser1` row
in `tenants` — `getCurrentTenant` was never called because no route
required tenant context.

This is a pre-beta blocker. We can't invite anyone until tenant-isolation
holds end-to-end.

## Goals

1. Every dashboard route that returns or mutates user-scoped data goes
   through `requireTenant(req)` and is scoped to that tenant.
2. Bot-API proxy routes (`/api/positions`, `/api/control/*`,
   `/api/events`, `/api/indicator-status`) hit the **calling tenant's**
   bots from `tenant_bots`, not operator's hardcoded containers.
3. SQL queries (`/api/trades` and other DB-backed routes) filter by
   `tenant_id`. Either via explicit `WHERE tenant_id = ?` or by
   connecting as the tenant's Postgres role and letting RLS enforce it.
4. Existing data (operator's trades / positions / equity snapshots / etc)
   gets `tenant_id` backfilled to operator's UUID, then NOT NULL flipped
   so future inserts can never be tenant-less.
5. Operator UX unchanged — operator is just tenant 1 with `is_operator=true`
   and three pre-existing `tenant_bots` rows.

## Non-goals

- Tenant CREATE flow (already done in Phase 2d via `/api/tenant/me/*`).
- Per-tenant container creation flow (already done in Phase 3a via
  `/api/tenant/me/bots`).
- Bot-side tenant tagging (Phase 3b — bots already tag their writes
  with their own `tenant_id` env var).
- Telegram routing (Phase 4, deferred).

## Key design decisions

### D1 — RLS connection vs query-level filter

**Choice: RLS connection.** When a tenant request lands, open the DB
pool as their `tenant_<32hex>` role (provisioned in Phase 5b). The RLS
policies from Phase 5a (`tenant_isolation` on 9 tables) auto-filter
SELECTs and validate INSERTs. Query code stays clean — no `WHERE
tenant_id = ?` bolted on every call.

Alternative considered: keep using the postgres superuser connection
and add `WHERE tenant_id = ?` to every query. Rejected because (a) it's
easy to forget on a new query → silent leak, (b) RLS already exists, (c)
it doesn't gain anything operationally.

Pool strategy: per-tenant pool cached for ~5 min, evicted on tenant lock
or on idle. ~5–20 active tenants in beta → small pool count.

### D2 — Bot routing: hardcoded vs tenant_bots lookup

**Choice: tenant_bots lookup.** Replace the env vars
`BOT_API_URL_PAPER/TESTNET/MAINNET` with a per-request lookup:
```ts
const bot = await db.select().from(tenantBots)
  .where(and(eq(tenantBots.tenantId, t.id), eq(tenantBots.mode, mode)))
  .limit(1);
if (!bot[0]) return NextResponse.json({error: "no bot for mode"}, {status: 404});
const url = bot[0].apiUrl; // e.g. "http://hypertrade-tenant-abc-paper:8000"
```

For operator (tenant 1), Phase 6b backfilled three `tenant_bots` rows
with `api_url` pointing at the existing `bot-paper`, `bot-testnet`,
`bot-mainnet` containers — so operator gets identical behavior.

For new tenants without any bots: 404 is the right answer. UI handles
it as "no bot started yet, go create one in Options".

### D3 — Backfill order on prod

Order matters because the data routes are about to start filtering.
If we deploy filtering BEFORE backfill, every existing row (NULL
tenant_id) becomes invisible — operator's dashboard goes blank.

```
Step 1 (DB only — no code change yet):
  alembic 0011: backfill tenant_id=<operator-uuid> WHERE tenant_id IS NULL
  on all 9 _TABLES_NEEDING_TENANT_ID tables.
  Then ALTER COLUMN tenant_id SET NOT NULL.
  Run on host: phase run -- bash -c '... alembic upgrade head'

Step 2 (code change — deploy to host):
  Dashboard routes call requireTenant + tenant-scoped DB pool +
  tenant_bots lookup. Bot containers unaffected.
```

Rolling back from step 2 = revert PR (routes go back to unscoped).
Rolling back from step 1 = need a manual `ALTER COLUMN ... DROP NOT NULL`
(reversible, just annoying). ZFS snapshot before running step 1 just
in case.

### D4 — Operator-only routes

Some endpoints stay operator-only:
- `/api/admin/*` (planned, doesn't exist yet)
- `/api/tls/*` — Caddy TLS config (host-level, single instance)
- `/api/control/*` for shared infra (none currently — all `/api/control/*`
  is per-bot which means per-tenant after this PR)

Add `requireOperator(req)` helper: throws 403 unless `tenant.isOperator
=== true` (Drizzle camelCase — DB column is `is_operator` but the row
shape from `typeof tenants.$inferSelect` uses the JS field name). Apply
to both TLS routes immediately.

### D5 — `/api/events` SSE stream

Currently subscribes to Redis `events:*` channel and forwards to all
connected clients. Has to filter by tenant_id in the event payload going
forward. Bot already includes `tenant_id` in events (Phase 3b), so filter
is one line: `if (event.tenant_id !== t.id) continue;`.

## Implementation plan

### Step 0 — verify pre-conditions

- [ ] Confirm operator's tenant UUID is `00000000-0000-0000-0000-000000000001`
- [ ] Confirm `tenant_bots` has 3 rows for operator (paper/testnet/mainnet)
- [ ] Confirm bot containers are emitting `tenant_id` in events / writes
  (spot check on `trades` table — should be NULL today, that's fine for
  now since backfill in step 1 will populate them)

### Step 1 — alembic 0011: backfill + NOT NULL

New migration `bot/alembic/versions/0011_backfill_tenant_id_not_null.py`:

```python
OPERATOR_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_TABLES = (
    "trades", "positions", "equity_snapshots", "funding_payments",
    "backtest_runs", "strategy_configs", "manual_onchain_levels",
    "hodl_purchases", "user_vault_entries",
)

def upgrade():
    for t in _TABLES:
        op.execute(
            f"UPDATE {t} SET tenant_id = '{OPERATOR_TENANT_ID}' "
            f"WHERE tenant_id IS NULL"
        )
        op.alter_column(t, "tenant_id", nullable=False)

def downgrade():
    for t in _TABLES:
        op.alter_column(t, "tenant_id", nullable=True)
        # Don't unbackfill — data stays tagged to operator on rollback.
```

Tests: pytest fixture inserts a NULL row pre-migration, verifies
backfilled to operator post-migration.

Deploy: ZFS snapshot first → one-shot alembic container → verify with
`SELECT count(*), tenant_id FROM trades GROUP BY tenant_id`.

### Step 2 — `requireOperator` + TLS gate

New file `dashboard/src/lib/operator.ts`:
```ts
export async function requireOperator(req: Request): Promise<Tenant> {
  const t = await requireTenant(req);
  if (!t.isOperator) {
    throw new Response(JSON.stringify({error: "operator only"}), {
      status: 403, headers: {"content-type": "application/json"},
    });
  }
  return t;
}
```

Wire into both `/api/tls/config` (GET) and `/api/tls/configure` (POST).
GET also requires operator because the dashboard proxies via `botFetch`
which forwards `API_KEY`, and the bot's `tls_get_config` handler is
auth-gated — making it dashboard-public would bypass that gate. The
returned payload also exposes the configured domain/email, which is
operator-only data.

### Step 3 — tenant-scoped DB pool factory

New file `dashboard/src/lib/db-tenant.ts`:
```ts
type TenantDb = ReturnType<typeof drizzle>;
type PoolEntry = {
  db: TenantDb;
  client: ReturnType<typeof postgres>;
  expiresAt: number;
};

const POOL_CACHE = new Map<string, PoolEntry>();
const POOL_TTL_MS = 5 * 60 * 1000;

export async function dbForTenant(
  tenant: Tenant,
  password: string,
): Promise<TenantDb> {
  const cached = POOL_CACHE.get(tenant.id);
  if (cached && cached.expiresAt > Date.now()) return cached.db;
  if (cached) cached.client.end({ timeout: 5 }); // evict expired

  const url = tenantDatabaseUrl(tenant.id, password); // Phase 5b
  const client = postgres(url, { max: 4, idle_timeout: 30 });
  const db = drizzle(client);
  POOL_CACHE.set(tenant.id, {
    db,
    client,
    expiresAt: Date.now() + POOL_TTL_MS,
  });
  return db;
}
```

Tenant role password: derived per tenant during Phase 5b (deterministic
HMAC of operator-side secret + tenant.id). Already implemented; wire it
into the pool factory.

Note: operator's pool uses operator's tenant role, NOT the postgres
superuser. Even operator goes through RLS now. (Operator's RLS policy
returns their UUID = data tagged with their tenant_id = visible.)

### Step 4 — bot routing via tenant_bots

Update `dashboard/src/lib/bot-api.ts`:
```ts
export async function getBotApiUrl(tenant: Tenant, mode: BotMode): Promise<string | null> {
  const rows = await db.select().from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenant.id), eq(tenantBots.mode, mode)))
    .limit(1);
  return rows[0]?.apiUrl ?? null;
}
```

Drop the env vars `BOT_API_URL_PAPER/TESTNET/MAINNET` from the
`dashboard` service in compose. Operator's `tenant_bots` rows already
have the right URLs from Phase 6b.

### Step 5 — gate every data route

Apply pattern to each affected route:
```ts
export async function GET(req: NextRequest) {
  const t = await requireTenant(req);  // throws 401
  const url = await getBotApiUrl(t, getMode(req));
  if (!url) return NextResponse.json({error: "no bot for mode"}, {status: 404});
  // ...existing fetch logic against `url` instead of env-derived URL
}
```

Routes to update (one PR per route group, not all at once):
- PR-A: `/api/positions`, `/api/indicator-status`
- PR-B: `/api/control/*` (pause, resume, flat-all, state, allow-multi-coin,
  config, strategy/[name]/leverage, strategy/[name]/toggle)
- PR-C: `/api/events` (SSE — needs filter logic too)
- PR-D: DB-backed routes — `/api/trades` (none currently exposed via `/api/`?
  Pages may query DB directly via server components. Audit first.)

### Step 6 — page-level audit

Server components that hit DB without going through API routes:
- `app/page.tsx` (overview)
- `app/trades/page.tsx`
- `app/strategies/page.tsx`
- `app/hodl/page.tsx`
- `app/vaults/page.tsx`

Pattern: replace direct `db.select(...)` with calls that pass through
`dbForTenant(t, password)`. Need passphrase-unlock for queries that need
it; for read-only public-ish data (vault snapshots), regular postgres
connection with explicit `WHERE tenant_id = ?`.

Audit findings → PR-E.

### Step 7 — end-to-end verification

After all PRs merged + deployed:
- [ ] Operator login → sees same data as before, no behavior change
- [ ] betauser1 fresh login (incognito):
  - Auto-creates tenant row via `getCurrentTenant`
  - `/positions` returns 404 "no bot for mode" (no bots yet)
  - `/trades` returns empty list
  - `/options` shows passphrase setup flow (Phase 2d)
  - Cannot see operator's data anywhere
- [ ] betauser1 sets passphrase, creates a paper bot via Options:
  - New container `hypertrade-tenant-<sub32>-paper` starts
  - `tenant_bots` row created
  - `/positions` now hits THEIR bot, returns empty (fresh bot)
- [ ] betauser1's bot does a paper trade:
  - Trade row inserted with `tenant_id = betauser1's UUID`
  - betauser1 sees the trade in `/trades`
  - operator does NOT see it (RLS enforces)

## Risks

- **Pool exhaustion**: 50 tenants × 4 connections = 200 connections.
  Postgres default `max_connections = 100`. Need to bump to 300 in
  postgres compose env or reduce per-pool max to 2.
- **First-login race**: `getCurrentTenant` auto-creates on first sight.
  If betauser1 lands on `/` and that page server-renders 4 parallel
  data fetches, we'd race 4× INSERT. Already handled by
  `onConflictDoNothing` in Phase 2c.
- **Operator's existing data** — backfill assumes ALL existing rows are
  operator's. True today (single-tenant deploy until now) but verify
  count before flipping NOT NULL. If any test row from migrations exists,
  it gets tagged to operator too, which is harmless.
- **SSE stream tenant filter** — bot must include `tenant_id` in every
  event. Verify in `bot/hypertrade/events/bus.py` before deploy. If it
  doesn't, the filter drops every event and dashboards go silent.

## Out of scope (future phases)

- Per-tenant resource limits (CPU/RAM caps on bot containers)
- Per-tenant rate limits on Hyperliquid API calls (one tenant burning
  the IP rate limit affects others)
- Operator admin UI to view/manage all tenants (Phase 9)
- Federated logout — Sign Out should also call Authentik
  end-session URL so SSO state clears (independent of 6c, but related)

## PR sequence

1. **PR α** — this plan doc, on master after signoff
2. **PR β** — alembic 0011 backfill + NOT NULL (Step 1) — DEPLOYED + VERIFIED before next PR
3. **PR γ** — `requireOperator` + TLS gate (Step 2)
4. **PR δ** — DB pool factory + bot routing helper (Steps 3+4) — no behavior change, infra only
5. **PR ε** — wire data routes (Step 5, all groups bundled OR one PR per group depending on diff size)
6. **PR ζ** — page-level audit + fixes (Step 6)
7. **PR η** — end-to-end verification + closed-beta launch checklist update
