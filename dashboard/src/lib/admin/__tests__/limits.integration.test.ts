/**
 * `reserveBotStart` against a real Postgres (testcontainers).
 *
 * The unit tests model the lock; this proves the model. Two concurrent
 * starts for one tenant with max_active_bots = 1 must let exactly one
 * through, which depends on real FOR UPDATE blocking and READ COMMITTED
 * visibility — the reservation has to be written before the lock is
 * released.
 *
 * The control case runs the pre-fix shape (check in one transaction,
 * reserve afterwards) and shows both getting through, so a regression
 * back to that shape is something this suite can see.
 *
 * Run with: `npm run test:integration` (needs Docker).
 */

import { randomUUID } from "node:crypto";

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { type PgFixture, seedTenants, setupPg } from "../../__tests__/_pg-test-fixture";

let fixture: PgFixture;
let limits: typeof import("../limits");
let dbMod: typeof import("../../db");
// One tenant per case, so the control's two rows can't affect the fix.
let tenantFixed: string;
let tenantControl: string;

const SUITE_TIMEOUT = 60_000;
// Held inside the transaction between the count and the reservation
// (or, for the control, between the check and the late write), so the
// second caller is guaranteed to arrive while the first is mid-flight.
const HOLD_SECONDS = 0.3;

beforeAll(async () => {
  fixture = await setupPg();
  const sql = fixture.operatorClient();
  // Full shape of the Drizzle `tenantBots` table — an insert names
  // every column.
  await sql.unsafe(`
    CREATE TABLE tenant_bots (
      id UUID PRIMARY KEY,
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      mode VARCHAR(16) NOT NULL,
      container_id VARCHAR(64),
      container_name VARCHAR(128),
      is_running BOOLEAN NOT NULL DEFAULT false,
      telegram_webhook_secret VARCHAR(64),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      last_started_at TIMESTAMPTZ,
      last_stopped_at TIMESTAMPTZ,
      UNIQUE (tenant_id, mode)
    );
  `);
  await sql.end();
  [tenantFixed, tenantControl] = await seedTenants(fixture, 2);

  process.env.DATABASE_URL = fixture.connectionString;
  vi.resetModules();
  dbMod = await import("../../db");
  limits = await import("../limits");
}, SUITE_TIMEOUT);

afterAll(async () => {
  await fixture?.stop();
}, SUITE_TIMEOUT);

async function runningCount(tenantId: string): Promise<number> {
  const sql = fixture.operatorClient();
  const rows = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM tenant_bots
    WHERE tenant_id = ${tenantId}::uuid AND is_running
  `;
  await sql.end();
  return rows[0].n;
}

function insertRunning(
  exec: { insert: typeof dbMod.db.insert },
  tenantId: string,
  mode: string,
) {
  return exec.insert(dbMod.tenantBots).values({
    id: randomUUID(),
    tenantId,
    mode,
    containerId: "claiming",
    isRunning: true,
  });
}

describe("reserveBotStart (real Postgres)", () => {
  it(
    "lets exactly one of two concurrent starts through a cap of 1",
    async () => {
      const tenantId = tenantFixed;
      const tenant = { id: tenantId, maxActiveBots: 1 };
      const { sql } = await import("drizzle-orm");

      const start = (mode: string) =>
        limits.reserveBotStart(tenant, async (tx) => {
          await tx.execute(sql`SELECT pg_sleep(${HOLD_SECONDS})`);
          await insertRunning(tx, tenantId, mode);
        });

      const results = await Promise.allSettled([
        start("paper"),
        start("testnet"),
      ]);

      expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(1);
      const rejected = results.filter(
        (r): r is PromiseRejectedResult => r.status === "rejected",
      );
      expect(rejected).toHaveLength(1);
      expect(rejected[0].reason).toBeInstanceOf(limits.LimitExceededError);
      expect(await runningCount(tenantId)).toBe(1);
    },
    SUITE_TIMEOUT,
  );

  it(
    "control: the pre-fix shape (check, then write) lets both through",
    async () => {
      const tenantId = tenantControl;
      const tenant = { id: tenantId, maxActiveBots: 1 };
      const { sql } = await import("drizzle-orm");

      const oldShapeStart = async (mode: string) => {
        // Check-only transaction, as assertCanStartBot was ...
        await limits.reserveBotStart(tenant, async () => undefined);
        // ... and the start becomes countable only later.
        await dbMod.db.execute(sql`SELECT pg_sleep(${HOLD_SECONDS})`);
        await insertRunning(dbMod.db, tenantId, mode);
      };

      const results = await Promise.allSettled([
        oldShapeStart("paper"),
        oldShapeStart("testnet"),
      ]);

      expect(results.every((r) => r.status === "fulfilled")).toBe(true);
      expect(await runningCount(tenantId)).toBe(2);
    },
    SUITE_TIMEOUT,
  );
});
