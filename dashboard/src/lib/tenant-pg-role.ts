/**
 * Per-tenant Postgres role helpers — multi-tenancy Phase 5b.
 *
 * Each tenant gets a Postgres role named `tenant_<32hex>` matching
 * the contract that alembic 0010's `app_tenant_id()` function decodes.
 * The bot container connects as that role; RLS policies (Phase 5a)
 * filter every query so the tenant can only see/write its own rows.
 *
 * v1 caveat: a fresh role password is generated on every bot-create,
 * overwriting any previous one. If a tenant ever has multiple bots
 * running concurrently (only operator does today via
 * multi_bot_enabled), the older bot's connection breaks on its next
 * reconnect. Acceptable for the closed-beta single-bot pattern;
 * multi-bot password sync gets a proper fix before
 * multi_bot_enabled is exposed to non-operator tenants.
 */

import { randomBytes } from "node:crypto";

import { sql } from "drizzle-orm";

import { db } from "./db";

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
 * a service-account credential that lives in container env vars and
 * is rotated on every bot-restart.
 */
export function generateRolePassword(): string {
  return randomBytes(32).toString("base64url");
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
];

/**
 * Subset of TENANT_DATA_TABLES that have a `serial id` column and
 * therefore an auto-generated `<table>_id_seq` sequence. Used to
 * scope sequence grants instead of granting on every sequence in
 * the public schema (which would include tenant_audit_log_id_seq
 * and other dashboard-only sequences).
 *
 * `user_vault_entries` is excluded — it has a composite PK on
 * (user_address, vault_address), no serial id, no sequence.
 */
const TENANT_DATA_TABLES_WITH_ID_SEQ = TENANT_DATA_TABLES.filter(
  (t) => t !== "user_vault_entries",
);

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
  const tablesList = TENANT_DATA_TABLES.join(", ");

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
  await db.execute(sql.raw(
    `GRANT SELECT, INSERT, UPDATE, DELETE ON ${tablesList} TO ${roleName};`,
  ));
  // Sequence grants must be per-table — `ALL SEQUENCES IN SCHEMA
  // public` would also grant the dashboard-only tables' sequences
  // (tenant_audit_log etc), which we DON'T want tenants to touch.
  // Postgres names auto-generated sequences `<table>_<col>_seq`;
  // each per-tenant table has exactly one for its `id` column.
  for (const table of TENANT_DATA_TABLES_WITH_ID_SEQ) {
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
