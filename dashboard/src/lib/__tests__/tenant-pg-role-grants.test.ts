/**
 * provisionRole SQL-shape tests (PR 84 follow-up).
 *
 * Verifies the GRANT statements provisionRole emits — specifically:
 *   - Tenant-scoped tables get full DML (SELECT/INSERT/UPDATE/DELETE).
 *   - Shared tables get SELECT/INSERT/UPDATE but NOT DELETE
 *     (vault data is global; a compromised tenant credential
 *     shouldn't be able to wipe history for all tenants).
 *   - Sequence grants cover both tenant + shared tables with
 *     autoincrement ids, excluding tables with composite/String PKs.
 *
 * Mocks `db.execute` so we don't need a live Postgres. Asserts on
 * the raw SQL strings — fragile to whitespace but that's the cost
 * of verifying actual SQL shape vs Drizzle's opaque AST.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock is hoisted; can't reference top-level vars inside the
// factory. Use vi.hoisted() to share the mock between the
// factory and the test bodies.
const { executeMock } = vi.hoisted(() => ({
  executeMock: vi.fn(),
}));
vi.mock("../db", () => ({
  db: { execute: executeMock },
}));

import { provisionRole } from "../tenant-pg-role";

beforeEach(() => {
  executeMock.mockResolvedValue(undefined);
});

const TENANT = "3a2f1e4c-aaaa-bbbb-cccc-dddd11112222";
const ROLE = "tenant_3a2f1e4caaaabbbbccccdddd11112222";

function executedSql(): string[] {
  // Drizzle's sql.raw wraps a string and exposes `queryChunks`
  // (or similar) — we just stringify the call arg and collect.
  return executeMock.mock.calls.map((args) => {
    const arg = args[0];
    if (typeof arg === "string") return arg;
    // sql.raw returns an SQL object with the literal in
    // `queryChunks[0].value` (Drizzle internal) — best-effort.
    if (arg && typeof arg === "object") return JSON.stringify(arg);
    return String(arg);
  });
}

afterEach(() => {
  executeMock.mockClear();
});

describe("provisionRole SQL shape", () => {
  it("issues separate GRANT statements for tenant-scoped vs shared tables", async () => {
    await provisionRole(TENANT, "test-pw");
    const all = executedSql().join("\n---\n");

    // Tenant data tables get full DML
    expect(all).toMatch(/GRANT SELECT, INSERT, UPDATE, DELETE ON[\s\S]+trades/);
    expect(all).toMatch(
      /GRANT SELECT, INSERT, UPDATE, DELETE ON[\s\S]+tenant_telegram_links/,
    );

    // Shared tables get SELECT/INSERT/UPDATE only (no DELETE)
    expect(all).toMatch(/GRANT SELECT, INSERT, UPDATE ON[\s\S]+vaults/);

    // The shared-tables GRANT must NOT include DELETE.
    // Find the exact statement among executed SQLs.
    const stmts = executedSql();
    const sharedGrantStmt = stmts.find(
      (s) =>
        s.includes("GRANT SELECT, INSERT, UPDATE ON") &&
        s.includes("vaults") &&
        !s.includes("DELETE"),
    );
    expect(sharedGrantStmt).toBeDefined();

    // Conversely, NO statement should grant DELETE on vault_*
    // tables — that's exactly the security gap PR 84's review
    // closed.
    const deleteOnSharedStmt = stmts.find(
      (s) =>
        s.includes("DELETE") &&
        (s.includes("vault_snapshots") || s.includes("vault_nav_history") ||
          (s.includes("vaults") && !s.includes("user_vault_entries"))),
    );
    expect(deleteOnSharedStmt).toBeUndefined();
  });

  it("includes vault_snapshots in sequence grants but excludes vaults + vault_nav_history", async () => {
    await provisionRole(TENANT, "test-pw");
    const all = executedSql().join("\n---\n");

    // vault_snapshots has serial id → sequence grant present
    expect(all).toContain(
      `GRANT USAGE, SELECT ON SEQUENCE vault_snapshots_id_seq TO ${ROLE}`,
    );
    // vaults has String PK (address) → no sequence grant
    expect(all).not.toContain("vaults_id_seq");
    // vault_nav_history has composite PK → no sequence grant
    expect(all).not.toContain("vault_nav_history_id_seq");
  });

  it("does not grant on dashboard-only tables", async () => {
    await provisionRole(TENANT, "test-pw");
    const all = executedSql().join("\n---\n");

    // tenant_audit_log + tenants + tenant_bots + tenant_secrets
    // are dashboard-only (operator's reach) — never grant on them.
    expect(all).not.toContain("tenant_audit_log_id_seq");
    expect(all).not.toMatch(/GRANT[\s\S]+tenants(?!_telegram_links)/);
    expect(all).not.toMatch(/GRANT[\s\S]+tenant_bots/);
    expect(all).not.toMatch(/GRANT[\s\S]+tenant_secrets/);
  });
});
