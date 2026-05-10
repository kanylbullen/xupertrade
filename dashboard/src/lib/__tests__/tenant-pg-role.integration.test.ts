/**
 * RLS + tenant role integration tests against a real Postgres
 * spawned via testcontainers (multi-tenancy Phase 5c).
 *
 * Excluded from default `npm test` (see vitest.config.ts) so
 * Docker-less dev machines can still run the unit suite. Run
 * explicitly via `npm run test:integration` — fails fast (clear
 * testcontainers error) when Docker isn't reachable.
 *
 * What this proves end-to-end:
 *   - provisionRole creates a real PG role with the right grants
 *   - tenant role's INSERTs land with their tenant_id
 *   - tenant role's SELECT cannot see another tenant's rows (RLS)
 *   - tenant role's INSERT WITH CHECK rejects another tenant's id
 *   - tenant role cannot touch the dashboard-managed tenants table
 *   - dropRole + re-provisionRole cycle is clean
 *
 * Run with: `npm run test:integration` (separate from `npm test`).
 */

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { type PgFixture, seedTenants, setupPg } from "./_pg-test-fixture";

let fixture: PgFixture;
let tenantA: string;
let tenantB: string;
// Imported dynamically AFTER process.env.DATABASE_URL is set so the
// dashboard's db.ts singleton connects to the testcontainer.
let mod: typeof import("../tenant-pg-role");

const SUITE_TIMEOUT = 60_000;

beforeAll(async () => {
  fixture = await setupPg();
  process.env.DATABASE_URL = fixture.connectionString;

  // db.ts initializes its postgres-js client at module-load time
  // from process.env.DATABASE_URL. We need vitest's module cache to
  // forget any prior load so the freshly-set env var takes effect
  // when tenant-pg-role.ts (transitively) imports db.ts.
  vi.resetModules();
  mod = await import("../tenant-pg-role");

  [tenantA, tenantB] = await seedTenants(fixture, 2);
}, SUITE_TIMEOUT);

afterAll(async () => {
  await fixture?.stop();
}, SUITE_TIMEOUT);

describe("provisionRole + RLS isolation (Phase 5c)", () => {
  it(
    "tenant role created with the right name + grants; can read+write own tenant_id rows",
    async () => {
      const passA = mod.generateRolePassword();
      const roleA = await mod.provisionRole(tenantA, passA);
      expect(roleA).toBe(`tenant_${tenantA.replace(/-/g, "")}`);

      // Connect as tenant A and write a row tagged with their id.
      const sqlA = fixture.tenantClient(roleA, passA);
      await sqlA`
        INSERT INTO trades (tenant_id, order_id, strategy_name, symbol, side, size, price)
        VALUES (${tenantA}::uuid, 'order-A1', 'bb_short', 'SOL', 'buy', 1.0, 100.0)
      `;
      const ownRows = await sqlA<{ order_id: string }[]>`
        SELECT order_id FROM trades
      `;
      expect(ownRows.map((r) => r.order_id)).toEqual(["order-A1"]);
    },
    SUITE_TIMEOUT,
  );

  it(
    "tenant A cannot see tenant B's rows (RLS USING)",
    async () => {
      const passA = mod.generateRolePassword();
      const passB = mod.generateRolePassword();
      const roleA = await mod.provisionRole(tenantA, passA);
      const roleB = await mod.provisionRole(tenantB, passB);

      const sqlA = fixture.tenantClient(roleA, passA);
      const sqlB = fixture.tenantClient(roleB, passB);

      await sqlB`
        INSERT INTO trades (tenant_id, order_id, strategy_name, symbol, side, size, price)
        VALUES (${tenantB}::uuid, 'order-B-secret', 'bb_short', 'SOL', 'buy', 99.9, 999.0)
      `;

      // Tenant A queries trades — must NOT see B's row.
      const rowsAseesAll = await sqlA<{ order_id: string }[]>`
        SELECT order_id FROM trades
      `;
      const allOrderIds = rowsAseesAll.map((r) => r.order_id);
      expect(allOrderIds).not.toContain("order-B-secret");
    },
    SUITE_TIMEOUT,
  );

  it(
    "tenant A INSERT with another tenant's id is rejected by WITH CHECK",
    async () => {
      const passA = mod.generateRolePassword();
      const roleA = await mod.provisionRole(tenantA, passA);
      const sqlA = fixture.tenantClient(roleA, passA);

      // Try to write a row tagged with tenantB's id. RLS WITH CHECK
      // should reject this even though tenantA has INSERT grant.
      await expect(
        sqlA`
          INSERT INTO trades (tenant_id, order_id, strategy_name, symbol, side, size, price)
          VALUES (${tenantB}::uuid, 'forged-by-A', 'bb_short', 'SOL', 'buy', 1, 1)
        `,
      ).rejects.toThrow(/row-level security/i);
    },
    SUITE_TIMEOUT,
  );

  it(
    "tenant role has no access to the dashboard-managed tenants table",
    async () => {
      const passA = mod.generateRolePassword();
      const roleA = await mod.provisionRole(tenantA, passA);
      const sqlA = fixture.tenantClient(roleA, passA);

      // We never granted SELECT on `tenants` to the tenant role —
      // attempt should fail with permission denied (not RLS — pure
      // GRANT-based).
      await expect(
        sqlA`SELECT id FROM tenants LIMIT 1`,
      ).rejects.toThrow(/permission denied/i);
    },
    SUITE_TIMEOUT,
  );

  it(
    "provisionRole is idempotent: second call rotates password without erroring",
    async () => {
      const pass1 = mod.generateRolePassword();
      const pass2 = mod.generateRolePassword();
      await mod.provisionRole(tenantA, pass1);
      // Second call with new password — must not throw duplicate_object.
      await expect(mod.provisionRole(tenantA, pass2)).resolves.toBeTruthy();

      // Old password no longer works.
      const roleA = `tenant_${tenantA.replace(/-/g, "")}`;
      const sqlOld = fixture.tenantClient(roleA, pass1);
      await expect(
        sqlOld`SELECT 1`,
      ).rejects.toThrow(/password authentication failed|SASL/i);
    },
    SUITE_TIMEOUT,
  );

  it(
    "dropRole removes the role + revokes grants",
    async () => {
      // Create a fresh tenant just for this test so the drop doesn't
      // cascade-affect the others.
      const [doomedTenant] = await seedTenants(fixture, 2);  // returns 2; we use [0]
      const pass = mod.generateRolePassword();
      const role = await mod.provisionRole(doomedTenant, pass);

      // Sanity: role exists.
      const op = fixture.operatorClient();
      const [{ exists }] = await op<{ exists: boolean }[]>`
        SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ${role}) AS exists
      `;
      expect(exists).toBe(true);

      await mod.dropRole(doomedTenant);

      const [{ exists: stillExists }] = await op<{ exists: boolean }[]>`
        SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ${role}) AS exists
      `;
      expect(stillExists).toBe(false);
      await op.end();
    },
    SUITE_TIMEOUT,
  );
});
