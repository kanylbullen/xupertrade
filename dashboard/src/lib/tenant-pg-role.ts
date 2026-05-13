/**
 * Per-tenant Postgres role helpers — multi-tenancy Phase 5b.
 *
 * Each tenant gets a Postgres role named `tenant_<32hex>` matching
 * the contract that alembic 0010's `app_tenant_id()` function decodes.
 * The bot container connects as that role; RLS policies (Phase 5a)
 * filter every query so the tenant can only see/write its own rows.
 *
 * Password lifecycle (revised 2026-05-13 — was per-bot rotation):
 *
 *   The tenant role's password is generated once and then **reused
 *   across all bots for the same tenant** (paper / testnet / mainnet).
 *   The password is cached in Redis at `tenant:<id>:pg_role_pw`.
 *
 *   The previous v1 implementation rotated the password on every
 *   bot-start. With three bots per tenant (the operator's standard
 *   layout) starting sequentially during compose-up, the last bot to
 *   start wins — the other two ran with stale DATABASE_URL env vars
 *   and silently failed every Postgres connection attempt. Order
 *   placement still succeeded (HL SDK is independent of DB) so the
 *   bots executed real trades the DB never saw, producing the exact
 *   divergence we built RLS to prevent. See incident 2026-05-13
 *   05:00–10:00 UTC; trades 192–197 placed on HL but never recorded.
 *
 *   Storing the password in Redis is acceptable because the operator
 *   already has DB-root and Redis-root on the same host — no trust
 *   boundary to cross. On Redis loss the next bot-start regenerates
 *   it; any concurrently-running bot for the same tenant should then
 *   be restarted to pick up the new value. In practice the operator
 *   restarts all three bots together via `docker compose up -d`.
 */

import { randomBytes } from "node:crypto";

import { sql } from "drizzle-orm";

import { db } from "./db";
import { getRedisClient } from "./redis";

/**
 * Role name for a given tenant UUID. Strips dashes + lowercases.
 *   3a2f1e4c-aaaa-bbbb-cccc-dddd11112222 → tenant_3a2f1e4caaaabbbbccccdddd11112222
 *
 * Postgres role names are case-folded by default but UUIDs are
 * already lowercase hex; explicit `.toLowerCase()` for safety.
 */
const HEX32 = /^[a-f0-9]{32}$/;

export function roleNameForTenant(tenantId: string): string {
  const hex = tenantId.replace(/-/g, "").toLowerCase();
  if (!HEX32.test(hex)) {
    throw new RangeError(
      `tenantId must be a 32-hex UUID (got ${JSON.stringify(hex)})`,
    );
  }
  return `tenant_${hex}`;
}

/**
 * 32-byte URL-safe base64 password, ≈42 chars. Plenty of entropy for
 * a service-account credential that lives in container env vars.
 *
 * Note: this no longer rotates per bot-start (see module-level docstring).
 * `getOrCreatePgRolePassword` is the high-level entrypoint; callers should
 * prefer that. Exported here for the integration tests.
 */
export function generateRolePassword(): string {
  return randomBytes(32).toString("base64url");
}

/** Redis key where the per-tenant role password is cached. */
export function pgRolePasswordKey(tenantId: string): string {
  // Validate format before letting the value reach Redis — defense
  // against caller bugs that would otherwise scatter junk keys.
  const hex = tenantId.replace(/-/g, "").toLowerCase();
  if (!HEX32.test(hex)) {
    throw new RangeError(
      `tenantId must be a 32-hex UUID (got ${JSON.stringify(hex)})`,
    );
  }
  return `tenant:${hex}:pg_role_pw`;
}

/**
 * Return the tenant role's password from Redis, creating it on first
 * access. Idempotent across concurrent callers via `SETNX`: only one
 * caller wins the SETNX and the others read back the winner's value.
 *
 * Combined with `provisionRole` (which is also idempotent via
 * CREATE-OR-ALTER), this means a bot-start for the operator's tenant
 * gets the same DATABASE_URL no matter how many sibling bots are
 * started concurrently — fixing the 2026-05-13 divergence incident.
 */
export async function getOrCreatePgRolePassword(
  tenantId: string,
): Promise<string> {
  const key = pgRolePasswordKey(tenantId);
  const redis = getRedisClient();
  const existing = await redis.get(key);
  if (existing && existing.length > 0) {
    return existing;
  }
  const fresh = generateRolePassword();
  // SETNX so a concurrent caller's value wins atomically. If we lose
  // the race, GET retrieves the winner; we throw away our fresh value
  // and use theirs. This keeps the DB role password consistent across
  // all parallel bot-starts.
  const won = await redis.set(key, fresh, "NX");
  if (won === "OK") {
    return fresh;
  }
  // Lost the race — read the winner.
  const winner = await redis.get(key);
  if (winner && winner.length > 0) {
    return winner;
  }
  // Pathological: SETNX claimed loss but GET sees nothing (Redis
  // eviction between calls). Fall back to ours and let the next caller
  // race again.
  await redis.set(key, fresh);
  return fresh;
}

/**
 * Force-rotate the tenant role's password. Used at tenant-delete time
 * (paired with dropRole) and reserved for explicit rotation. NOT
 * called on bot-start anymore — see module docstring.
 */
export async function forgetPgRolePassword(tenantId: string): Promise<void> {
  await getRedisClient().del(pgRolePasswordKey(tenantId));
}

/**
 * Tables a tenant role needs read+write access to. Mirrors alembic
 * 0009's `_TABLES_NEEDING_TENANT_ID` minus the dashboard-only tables
 * (tenants, tenant_bots, tenant_secrets, tenant_audit_log) which the
 * tenant must NEVER touch directly.
 */
const TENANT_DATA_TABLES = [
  "trades",
  "positions",
  "equity_snapshots",
  "funding_payments",
  "backtest_runs",
  "strategy_configs",
  "manual_onchain_levels",
  "hodl_purchases",
  "user_vault_entries",
  // PR 3b: bot's /link handler upserts here; get-by-chat lookups
  // also happen bot-side. RLS policy (alembic 0014) restricts
  // each role to its own tenant_id rows — without that, a SELECT
  // by a tenant role would leak every other tenant's chat_id +
  // username. The PK on tenant_id + UNIQUE on chat_id (alembic
  // 0013) prevent dupes/spoofing but DO NOT provide isolation;
  // RLS is the mechanism that does.
  "tenant_telegram_links",
];

/**
 * Tables that are global (no tenant_id column, no RLS) but the
 * bot still needs read+write access to. Vault data is shared:
 * all tenants see the same HyperLiquid public vaults; the daily
 * scanner upserts metadata + snapshots + NAV history regardless
 * of which tenant happens to be polling. No isolation needed
 * (it's public chain data); the bot just needs the grant.
 *
 * Without this list the tenant role hits
 * `permission denied for table vaults` whenever the vault
 * scanner ticks (every 30 min for the testnet bot per the
 * vault-scanner Phase 1 plan).
 */
const SHARED_TABLES = [
  "vaults",
  "vault_snapshots",
  "vault_nav_history",
];

/**
 * Tables (from BOTH TENANT_DATA_TABLES and SHARED_TABLES) that have
 * a `serial id` column and therefore an auto-generated
 * `<table>_id_seq` sequence. Used to scope sequence grants instead
 * of granting on every sequence in the public schema (which would
 * include tenant_audit_log_id_seq and other dashboard-only
 * sequences).
 *
 * Excluded tables:
 *   - `user_vault_entries`: composite PK on (user_address,
 *     vault_address), no serial id, no sequence.
 *   - `tenant_telegram_links`: PK is `tenant_id` (UUID), no
 *     serial id, no sequence.
 *   - `vaults`: PK is `address` (String), no sequence.
 *   - `vault_nav_history`: composite PK (vault_address, timestamp),
 *     no sequence.
 *
 * Granting on a non-existent sequence raises "relation does not
 * exist" and breaks provisionRole() → bot startup fails for new
 * tenants. The filter list must stay in sync with table-creation
 * migrations.
 */
const TABLES_WITHOUT_ID_SEQ = new Set([
  "user_vault_entries",
  "tenant_telegram_links",
  "vaults",
  "vault_nav_history",
]);
const TABLES_WITH_ID_SEQ = [
  ...TENANT_DATA_TABLES,
  ...SHARED_TABLES,
].filter((t) => !TABLES_WITHOUT_ID_SEQ.has(t));

/**
 * Idempotent: creates the role if missing, otherwise rotates its
 * password. Grants schema USAGE + DML on the data tables + USAGE on
 * sequences (for autoincrement IDs). Safe to call on every bot-create.
 *
 * Postgres identifiers can't be parameterised, so the role-name and
 * password are interpolated as string literals. They're caller-
 * controlled (we generate the password ourselves) but we still
 * defensive-validate the role-name shape and quote the password
 * literal explicitly.
 */
export async function provisionRole(
  tenantId: string,
  password: string,
): Promise<string> {
  const roleName = roleNameForTenant(tenantId);
  // Defense: roleName must match the strict pattern even though we
  // built it ourselves above. Catches any future hand-edit that
  // weakens the regex.
  if (!/^tenant_[a-f0-9]{32}$/.test(roleName)) {
    throw new Error(`invalid role name: ${roleName}`);
  }
  // Postgres password literal: single-quote-escape any quote in the
  // password. Since we generate base64url ourselves there shouldn't
  // be any, but defense-in-depth.
  const pwLiteral = password.replace(/'/g, "''");
  const tenantTablesList = TENANT_DATA_TABLES.join(", ");
  const sharedTablesList = SHARED_TABLES.join(", ");

  // CREATE-OR-ALTER pattern (race-safe). The naive `IF NOT EXISTS`
  // check has a TOCTOU race: two concurrent provisionRole() calls
  // can both pass the check, then collide on CREATE ROLE.
  // EXCEPTION-based pattern lets one win the CREATE and the other
  // catch `duplicate_object` then fall through to ALTER ROLE.
  await db.execute(sql.raw(`
    DO $$ BEGIN
      BEGIN
        CREATE ROLE ${roleName} LOGIN PASSWORD '${pwLiteral}';
      EXCEPTION WHEN duplicate_object THEN
        ALTER ROLE ${roleName} WITH PASSWORD '${pwLiteral}';
      END;
    END $$;
  `));
  // Grants are idempotent — re-running them is safe.
  await db.execute(sql.raw(`GRANT USAGE ON SCHEMA public TO ${roleName};`));
  // Per-tenant data tables: full DML. RLS (alembic 0010 + 0014)
  // restricts each role to its own tenant_id rows, so DELETE here
  // only nukes the tenant's own data.
  await db.execute(sql.raw(
    `GRANT SELECT, INSERT, UPDATE, DELETE ON ${tenantTablesList} TO ${roleName};`,
  ));
  // Shared tables (vault scanner data): NO DELETE. Vault data is
  // global; a tenant role with DELETE could wipe all tenants'
  // vault history. The scanner only needs upsert (INSERT/UPDATE)
  // semantics, so omitting DELETE costs us nothing and prevents
  // a compromised tenant credential from doing collateral damage.
  await db.execute(sql.raw(
    `GRANT SELECT, INSERT, UPDATE ON ${sharedTablesList} TO ${roleName};`,
  ));
  // Sequence grants must be per-table — `ALL SEQUENCES IN SCHEMA
  // public` would also grant the dashboard-only tables' sequences
  // (tenant_audit_log etc), which we DON'T want tenants to touch.
  // Postgres names auto-generated sequences `<table>_<col>_seq`;
  // each granted table with an autoincrement id has exactly one.
  for (const table of TABLES_WITH_ID_SEQ) {
    await db.execute(sql.raw(
      `GRANT USAGE, SELECT ON SEQUENCE ${table}_id_seq TO ${roleName};`,
    ));
  }
  return roleName;
}

/**
 * DROP a tenant's role (used at tenant-delete time, never at
 * bot-stop). Idempotent. Drops privileges first so the DROP itself
 * succeeds without "role still has assigned privileges" errors.
 */
export async function dropRole(tenantId: string): Promise<void> {
  const roleName = roleNameForTenant(tenantId);
  if (!/^tenant_[a-f0-9]{32}$/.test(roleName)) {
    throw new Error(`invalid role name: ${roleName}`);
  }
  await db.execute(sql.raw(`
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${roleName}') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ${roleName};
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ${roleName};
        REVOKE USAGE ON SCHEMA public FROM ${roleName};
        DROP ROLE ${roleName};
      END IF;
    END $$;
  `));
  // Also clear the cached password so a future tenant with the same
  // (cosmically unlikely) UUID gets a fresh one rather than reusing
  // an old role's value.
  try {
    await forgetPgRolePassword(tenantId);
  } catch {
    // Redis hiccup at delete time isn't fatal — the next bot-start
    // for this tenant will see the role missing and re-provision.
  }
}

/**
 * Build the DATABASE_URL the tenant bot uses. Always points at the
 * same Postgres host as the dashboard (internal docker network),
 * just with the tenant's role + freshly-generated password.
 *
 * Scheme is forced to `postgresql+asyncpg` (the bot uses
 * SQLAlchemy async via asyncpg). The dashboard's own DATABASE_URL
 * uses the libpq-compatible `postgresql` scheme for postgres-js;
 * if we forwarded that scheme verbatim, the bot would crash on
 * startup with "no such driver: postgresql" (PR #46 review fix).
 *
 * Query params (e.g. `?sslmode=require`) on the dashboard URL are
 * preserved so a future TLS-enabled deploy doesn't have to rebuild
 * this URL by hand.
 */
const ASYNCPG_SCHEME = "postgresql+asyncpg";

export function tenantDatabaseUrl(
  tenantId: string,
  password: string,
): string {
  const role = roleNameForTenant(tenantId);
  // Pull host/db parts from the dashboard's existing DATABASE_URL —
  // single source of truth for which Postgres we're talking to.
  const baseUrl =
    process.env.DATABASE_URL ??
    "postgresql://postgres:postgres@postgres:5432/hypertrade";
  const parsed = new URL(baseUrl);
  const search = parsed.search ?? "";
  return `${ASYNCPG_SCHEME}://${role}:${encodeURIComponent(password)}@${parsed.host}${parsed.pathname}${search}`;
}
