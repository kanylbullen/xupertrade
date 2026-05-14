# Plan — Operator admin page (`/admin`)

## Goal

A single page only the operator can reach that gives them a complete operational view of the system: every tenant, their bots, their strategies, P&L, and the host's resource state — plus per-tenant policy controls (limits, strategy allowlist).

## Out of scope

- Cross-tenant action audit trail (separate PR if needed; for now operator changes are visible in DB modified-at columns).
- Multi-host fan-out (this is single-host; if/when we shard, the page can grow a host-picker).
- Time-series charts for server stats (current snapshot only — ring buffer is a follow-up if useful).
- Editing tenant secrets, passphrase reset, or anything that crosses the encryption trust boundary. The operator can't read or alter tenant private keys; that property is preserved.

## Architecture

```
/admin                              ← server component, gated by requireOperatorTenant()
├── /admin                          ← Overview (tenant list + global counters)
├── /admin/[tenantId]               ← Tenant detail (bots, strategies, limits, P&L)
└── /admin/server                   ← Server stats (CPU/RAM/disk)

/api/admin/...                      ← all routes wrapped by requireOperatorTenant()
├── GET  /api/admin/tenants                     ← list with status counts
├── GET  /api/admin/tenants/[id]                ← detail (bots, P&L, limits)
├── PATCH /api/admin/tenants/[id]/limits        ← update caps + allowlist
└── GET  /api/admin/server-stats                ← /proc + df snapshot
```

Sidebar gets a new "Admin" link rendered conditionally on `me.isOperator === true` (same gate the existing user-menu uses for the operator badge).

## Data model

### New columns on `tenants`

```sql
ALTER TABLE tenants
  ADD COLUMN max_active_bots INTEGER,        -- NULL = unlimited
  ADD COLUMN max_active_strategies INTEGER,  -- NULL = unlimited
  ADD COLUMN allowed_strategies TEXT[];      -- NULL = all, [] = none, otherwise allowlist
```

Alembic migration `0016_tenant_admin_limits.py`. No backfill — NULL on existing rows preserves current behavior (no caps, all strategies visible).

### Why these defaults

- `NULL = unlimited` for the integer caps so existing tenants don't suddenly get clamped to zero.
- `allowed_strategies NULL = all` for backwards compat. Empty array `[]` is a meaningful "explicitly block everything" — useful for paused/banned tenants without revoking the account.
- Validation at write: `max_active_bots BETWEEN 0 AND 10`, `max_active_strategies BETWEEN 0 AND 30`. Strategy names in `allowed_strategies` must match the bot's registered set; we validate against the bot's `/api/strategies` snapshot at PATCH time.

## Enforcement points

### Bot Start (`POST /api/tenant/me/bots/[id]/start`)

```typescript
const tenant = await getTenantById(tenantId);
const activeBotsCount = await db.select({ count: count() })
  .from(tenantBots)
  .where(and(eq(tenantBots.tenantId, tenantId), eq(tenantBots.isActive, true)));

if (tenant.maxActiveBots !== null && activeBotsCount >= tenant.maxActiveBots) {
  return Response.json(
    { error: "active_bot_limit_reached", limit: tenant.maxActiveBots, current: activeBotsCount },
    { status: 409 },
  );
}
```

### Strategy enable (`POST /api/control/strategy/<name>/toggle`)

Bot side, via dashboard proxy. Two checks:
1. Strategy name is in `tenant.allowed_strategies` (or allowlist is NULL).
2. Active strategy count after toggle ≤ `tenant.max_active_strategies` (or NULL).

Both return 409 with a structured body so the UI can render a precise message. Existing over-limit strategies are NOT auto-disabled (grandfathered) — they continue running until tenant pauses them. The PATCH endpoint that lowers a limit returns a warning showing how many bots/strategies are currently over.

### Bot start-up (Python side)

The bot reads `allowed_strategies` from its own `tenant_secrets`-style join at startup and skips registering disallowed strategies. This is defense-in-depth — even if dashboard enforcement is bypassed, the bot won't load forbidden strategies.

## UI structure

### `/admin` (overview)

Single table:

| Tenant | Email | Bots (active/total) | Strategies (active) | Trades 7d | P&L 7d | Limits | Last seen |
|---|---|---|---|---|---|---|---|

- Each row links to `/admin/[tenantId]`.
- Color-coding: red dot if any bot reports "Offline" in the last 5 min, yellow if "Paused", green if all running.
- Sort defaults by Last seen desc.
- "Tenants over limit" filter pill at top — shows only rows that would fail the current cap.

### `/admin/[tenantId]` (detail)

Sections:

1. **Identity** — email, created date, last login, isOperator badge.
2. **Bots** — three cards (paper/testnet/mainnet) with status, container ID, last heartbeat, restart action (operator can force-stop a bot via this page; useful when tenant is unavailable).
3. **Active strategies** — list with toggles (operator can disable on tenant's behalf in an emergency).
4. **Limits & allowlist** — form with `max_active_bots`, `max_active_strategies`, multi-select `allowed_strategies` (defaults to "all" via NULL). Save → PATCH `/api/admin/tenants/[id]/limits`.
5. **P&L summary** — total realized 7d/30d/all-time, per-mode breakdown.
6. **Recent trades** — last 20, link to `/trades` filtered by this tenant.

### `/admin/server`

One card per metric:
- **CPU** — load average (1/5/15 min), core count, % usage from `/proc/stat` diff
- **RAM** — used/free/cached from `/proc/meminfo`
- **Disk** — `df -h /` for root, plus `/var/lib/docker` if separate
- **Docker** — container count + `docker stats --no-stream` summary (running container count, total CPU, total mem)

Polled every 5s from the client (lightweight `/api/admin/server-stats` endpoint).

## API routes

All under `requireOperatorTenant()` (same helper as existing operator routes — server-side 403 if not operator).

```
GET  /api/admin/tenants
     → { tenants: [{ id, email, displayName, isOperator, createdAt,
                     activeBotsCount, totalBotsCount, activeStrategiesCount,
                     trades7d, pnl7d, lastSeenAt,
                     limits: { maxActiveBots, maxActiveStrategies, allowedStrategies } }] }

GET  /api/admin/tenants/[id]
     → { tenant, bots: [...], strategies: [...], pnl: { 7d, 30d, all }, recentTrades: [...] }

PATCH /api/admin/tenants/[id]/limits
     body: { maxActiveBots: number|null,
             maxActiveStrategies: number|null,
             allowedStrategies: string[]|null }
     → 200 { limits, warnings: [{ kind: "bots_over_cap", current: 5, limit: 3 }, ...] }
     → 400 if validation fails (negative numbers, unknown strategy names)

GET  /api/admin/server-stats
     → { cpu: { loadAvg: [1,5,15], cores, usagePct },
         memory: { totalMB, usedMB, freeMB, cachedMB },
         disk: [{ mount, totalGB, usedGB, freeGB, usePct }],
         docker: { running, total, totalCpuPct, totalMemMB } }
```

## Sub-PR breakdown

Recommendation: ship as **one PR** rather than splitting. The pieces are tightly coupled (limits enforcement requires the schema change requires the migration, and the UI is meaningless without the API), and the total surface is moderate (~12 files, ~600 lines including tests). Splitting would multiply review overhead.

**If we split anyway** (only if PR feels too big when implemented):

- **PR A** (foundation): alembic migration + schema + `requireOperatorTenant()` already exists, plus the `/api/admin/tenants` and `/api/admin/server-stats` GET endpoints. Closed without UI.
- **PR B** (enforcement): `/api/tenant/me/bots/[id]/start` cap check, strategy toggle cap check. Adds tests.
- **PR C** (UI): `/admin/*` pages + sidebar link.

Default plan is single PR.

## Files changed

### New

- `bot/alembic/versions/0016_tenant_admin_limits.py`
- `dashboard/src/app/admin/layout.tsx` — operator gate (server component, 403 if not operator)
- `dashboard/src/app/admin/page.tsx` — overview
- `dashboard/src/app/admin/[tenantId]/page.tsx` — detail
- `dashboard/src/app/admin/server/page.tsx` — server stats
- `dashboard/src/app/api/admin/tenants/route.ts`
- `dashboard/src/app/api/admin/tenants/[id]/route.ts`
- `dashboard/src/app/api/admin/tenants/[id]/limits/route.ts`
- `dashboard/src/app/api/admin/server-stats/route.ts`
- `dashboard/src/lib/admin/server-stats.ts` — `/proc` + `df` parsing
- `dashboard/src/lib/admin/limits.ts` — enforcement helpers (`assertCanStartBot`, `assertCanEnableStrategy`)
- `dashboard/src/components/admin/*.tsx` — cards/tables (TenantTable, ServerStatsCard, LimitsForm)
- Tests for each of the above (vitest)

### Modified

- `dashboard/src/lib/db.ts` — add the 3 new columns to drizzle `tenants` schema
- `dashboard/src/components/app-sidebar.tsx` — conditional "Admin" link
- `dashboard/src/app/api/tenant/me/bots/[id]/start/route.ts` — call `assertCanStartBot()`
- bot-side strategy registry / startup — read `allowed_strategies` and skip-register disallowed
- `bot/hypertrade/db/repo.py` — query helper for `allowed_strategies`
- `CLAUDE.md` — note operator admin page exists

### Tests

- `dashboard/src/lib/admin/__tests__/server-stats.test.ts` — fixture `/proc/stat`, `/proc/meminfo`, `df` outputs → assert parsing.
- `dashboard/src/lib/admin/__tests__/limits.test.ts` — `assertCanStartBot` returns void on under-cap, throws structured error on at-cap, no-op on NULL cap.
- `dashboard/src/app/api/admin/__tests__/tenants.test.ts` — operator gate (403 for non-operator), data shape, count aggregations correct.
- `dashboard/src/app/api/admin/__tests__/limits.test.ts` — PATCH validates ranges, rejects unknown strategy names, returns over-cap warnings.
- `bot/tests/test_strategies/test_allowlist.py` — bot startup skips disallowed strategies.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Operator accidentally clamps own tenant to 0 bots and locks themselves out of trading | UI shows "you are editing your own tenant — this will affect your live bots" warning before save. Operator can always edit via DB if truly stuck. |
| `/proc` parsing differs across kernel versions (rare but happens for `/proc/stat` field count) | Defensive parsing — split on whitespace, check field count, log + return zeros if unexpected. Test with fixture from current kernel. |
| Server-stats endpoint is polled every 5s — adds load | `/proc` reads are <1ms; `df` is ~5ms. Single-tenant operator polling is negligible. If multiple operator sessions ever exist, add a 2s in-memory cache. |
| Allowlist change while bot is running doesn't auto-revoke active strategies | Documented behavior — change takes effect at next strategy toggle / bot restart. Operator can force-toggle via the detail page if urgent. |
| `allowed_strategies` is a TEXT[] which Drizzle handles awkwardly | Use `.array()` modifier; tested against the `pg` driver. Alternative is a JSON column if drizzle issues. |
| Operator can see tenant emails (PII) | Acceptable — operator role is by design the system admin. Document in CLAUDE.md. |

## Workflow

1. Branch: `feat/operator-admin-page`.
2. Implement schema + migration first; run `alembic upgrade head` locally.
3. Build API routes + tests, then UI.
4. Wire enforcement points (bot start, strategy toggle).
5. Bot-side allowlist read at startup.
6. `cd dashboard && bun run test && bunx tsc --noEmit && cd ../bot && uv run pytest -x`.
7. Commit, push, PR with this plan linked.
8. Copilot review → fix → merge → deploy.
9. Apply migration on prod (manual SQL like 0015, since alembic-via-bot-image can't auth as superuser).
10. Operator validates by visiting `/admin` on prod.
