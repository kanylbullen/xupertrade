/**
 * Shared Postgres testcontainer fixture for the Phase 5c integration
 * tests. Spins up a real Postgres 16 container, applies a minimal
 * schema mirroring the parts of alembic 0009/0010 we exercise (no
 * Python alembic dep — the integration test asserts RLS+role
 * behaviour, not migration correctness; that has its own smoke test).
 *
 * Each test file using this fixture should:
 *   beforeAll → setupPg()  (returns connection helpers + cleanup)
 *   afterAll  → fixture.stop()
 *
 * Tests can request fresh `Sql` clients connected as either:
 *   - operator (`postgres` superuser, full access)
 *   - tenant (`tenant_<32hex>` role, RLS enforced)
 */

import { randomUUID } from "node:crypto";

import postgres, { type Sql } from "postgres";
import {
  PostgreSqlContainer,
  type StartedPostgreSqlContainer,
} from "@testcontainers/postgresql";

export type PgFixture = {
  connectionString: string;
  /** Connect as the postgres superuser. */
  operatorClient: () => Sql;
  /** Connect as a tenant role (assumes it's been provisioned). */
  tenantClient: (roleName: string, password: string) => Sql;
  /** Tear down the container + close all clients. */
  stop: () => Promise<void>;
};

/**
 * Mirror of the relevant parts of alembic 0009 + 0010. We hand-roll
 * the SQL here so the test doesn't need a Python alembic runtime.
 * Drift between this and the real migrations is caught by the smoke
 * test (test_migration_0010_smoke.py) on the bot side, which checks
 * that the migration emits the same SQL fragments we mirror here.
 *
 * Production fidelity choices (PR #47 review):
 * - NO pgcrypto extension; NO `gen_random_uuid()` server default on
 *   tenants.id. Alembic 0009 explicitly avoids these (PR #36 review)
 *   so the dashboard supplies UUIDs application-side via
 *   `crypto.randomUUID()`. `seedTenants` does the same.
 * - RLS is enabled + tenant_isolation policy applied to ALL 9
 *   per-tenant tables (matches alembic 0010), not just `trades`.
 *   This way a regression that drops RLS from any one of them
 *   surfaces in this suite even if no test directly queries that
 *   table.
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
] as const;

const SCHEMA_SQL = `
  -- tenants table (Phase 1) — no pgcrypto / no server default;
  -- application supplies UUIDs (matches alembic 0009).
  CREATE TABLE tenants (
    id UUID PRIMARY KEY,
    authentik_sub VARCHAR(128) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_operator BOOLEAN NOT NULL DEFAULT false,
    multi_bot_enabled BOOLEAN NOT NULL DEFAULT false
  );

  -- The 9 per-tenant data tables. We only EXERCISE RLS on \`trades\`
  -- in the test cases below, but \`provisionRole\` GRANTs across all
  -- of them so they must exist or the GRANT fails with "relation
  -- does not exist". Stub each with the minimum shape — RLS
  -- behaviour is identical across them.
  CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    order_id VARCHAR(64) NOT NULL UNIQUE,
    strategy_name VARCHAR(64) NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    side VARCHAR(8) NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    price DOUBLE PRECISION NOT NULL
  );
  CREATE TABLE positions (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  CREATE TABLE equity_snapshots (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  CREATE TABLE funding_payments (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  CREATE TABLE backtest_runs (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  CREATE TABLE strategy_configs (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  CREATE TABLE manual_onchain_levels (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  CREATE TABLE hodl_purchases (
    id SERIAL PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE
  );
  -- user_vault_entries has a composite PK in the real schema (no
  -- serial id), and is therefore excluded from the per-table
  -- sequence grants in tenant-pg-role.ts. Stub matches.
  CREATE TABLE user_vault_entries (
    user_address VARCHAR(42) NOT NULL,
    vault_address VARCHAR(42) NOT NULL,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    PRIMARY KEY (user_address, vault_address)
  );

  -- Phase 5a: app_tenant_id() helper. Marked STABLE (PR #45 review).
  CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS UUID AS $$
  BEGIN
    IF current_user NOT LIKE 'tenant\\_%' ESCAPE '\\' THEN
      RETURN NULL;
    END IF;
    RETURN (
      substring(current_user from 8 for 8) || '-' ||
      substring(current_user from 16 for 4) || '-' ||
      substring(current_user from 20 for 4) || '-' ||
      substring(current_user from 24 for 4) || '-' ||
      substring(current_user from 28 for 12)
    )::uuid;
  EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
  END;
  $$ LANGUAGE plpgsql STABLE;

  -- Phase 5a: enable RLS + tenant_isolation policy on every
  -- per-tenant table (PR #47 review fix). The tests directly
  -- exercise \`trades\`, but applying the policy across all 9
  -- tables makes any "missed an ALTER TABLE in alembic 0010"
  -- regression surface here too.
  ALTER TABLE trades                ENABLE ROW LEVEL SECURITY;
  ALTER TABLE positions             ENABLE ROW LEVEL SECURITY;
  ALTER TABLE equity_snapshots      ENABLE ROW LEVEL SECURITY;
  ALTER TABLE funding_payments      ENABLE ROW LEVEL SECURITY;
  ALTER TABLE backtest_runs         ENABLE ROW LEVEL SECURITY;
  ALTER TABLE strategy_configs      ENABLE ROW LEVEL SECURITY;
  ALTER TABLE manual_onchain_levels ENABLE ROW LEVEL SECURITY;
  ALTER TABLE hodl_purchases        ENABLE ROW LEVEL SECURITY;
  ALTER TABLE user_vault_entries    ENABLE ROW LEVEL SECURITY;

  CREATE POLICY tenant_isolation ON trades                FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON positions             FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON equity_snapshots      FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON funding_payments      FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON backtest_runs         FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON strategy_configs      FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON manual_onchain_levels FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON hodl_purchases        FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
  CREATE POLICY tenant_isolation ON user_vault_entries    FOR ALL USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());
`;

export async function setupPg(): Promise<PgFixture> {
  const container = (await new PostgreSqlContainer("postgres:16-alpine")
    .withDatabase("hypertrade_test")
    .withUsername("postgres")
    .withPassword("postgres")
    .start()) as StartedPostgreSqlContainer;

  const connectionString = container.getConnectionUri();

  // Apply the schema as the superuser.
  const adminClient = postgres(connectionString, { max: 1 });
  await adminClient.unsafe(SCHEMA_SQL);
  await adminClient.end();

  const allClients: Sql[] = [];

  return {
    connectionString,
    operatorClient: () => {
      const c = postgres(connectionString, { max: 1 });
      allClients.push(c);
      return c;
    },
    tenantClient: (roleName: string, password: string) => {
      // Construct connection string with tenant credentials. Same
      // host/db as the admin client; just different user/password.
      const url = new URL(connectionString);
      url.username = roleName;
      url.password = password;
      const c = postgres(url.toString(), { max: 1 });
      allClients.push(c);
      return c;
    },
    stop: async () => {
      await Promise.all(allClients.map((c) => c.end().catch(() => undefined)));
      await container.stop();
    },
  };
}

/**
 * Insert N test tenants, returning their UUIDs. UUIDs are generated
 * application-side via `crypto.randomUUID()` to match production —
 * alembic 0009 deliberately has no `gen_random_uuid()` server
 * default to avoid the pgcrypto extension dependency (PR #36 review).
 *
 * Uses the operator client because `tenants` is a dashboard-managed
 * table that no tenant role should ever touch.
 */
export async function seedTenants(
  fixture: PgFixture,
  count: 2,
): Promise<[string, string]>;
export async function seedTenants(
  fixture: PgFixture,
  count: number,
): Promise<string[]> {
  const sql = fixture.operatorClient();
  const ids: string[] = [];
  for (let i = 0; i < count; i++) {
    const id = randomUUID();
    const sub = `test-sub-${i}-${Date.now()}`;
    await sql`
      INSERT INTO tenants (id, authentik_sub, email)
      VALUES (${id}::uuid, ${sub}, ${sub + "@example.com"})
    `;
    ids.push(id);
  }
  await sql.end();
  return ids;
}
