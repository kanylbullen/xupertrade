/**
 * Unit tests for the tenant Postgres role helpers (multi-tenancy
 * Phase 5b). DB-touching functions (`provisionRole`, `dropRole`) are
 * covered by Phase 5c integration tests against a real Postgres —
 * here we test only the pure helpers and the URL builder.
 */

import { describe, expect, it } from "vitest";

import {
  generateRolePassword,
  roleNameForTenant,
  tenantDatabaseUrl,
} from "../tenant-pg-role";

const TENANT = "3a2f1e4c-aaaa-bbbb-cccc-dddd11112222";

describe("roleNameForTenant", () => {
  it("strips dashes and produces tenant_<32hex>", () => {
    expect(roleNameForTenant(TENANT)).toBe(
      "tenant_3a2f1e4caaaabbbbccccdddd11112222",
    );
  });

  it("matches alembic 0010's role-name pattern (regex pin)", () => {
    expect(roleNameForTenant(TENANT)).toMatch(/^tenant_[a-f0-9]{32}$/);
  });

  it("lowercases mixed-case input", () => {
    const mixed = "3A2F1E4C-AAAA-BBBB-CCCC-DDDD11112222";
    expect(roleNameForTenant(mixed)).toBe(
      "tenant_3a2f1e4caaaabbbbccccdddd11112222",
    );
  });

  it("throws RangeError on too-short id", () => {
    expect(() => roleNameForTenant("short")).toThrow(/32-hex UUID/);
  });

  it("throws RangeError on too-long id", () => {
    expect(() =>
      roleNameForTenant(TENANT + "extra"),
    ).toThrow(/32-hex UUID/);
  });
});

describe("generateRolePassword", () => {
  it("returns a base64url string", () => {
    const pw = generateRolePassword();
    expect(typeof pw).toBe("string");
    // base64url alphabet: A-Z a-z 0-9 - _
    expect(pw).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("is at least 32 characters (32 raw bytes encoded)", () => {
    const pw = generateRolePassword();
    expect(pw.length).toBeGreaterThanOrEqual(32);
  });

  it("two calls produce different passwords (cryptographic randomness)", () => {
    expect(generateRolePassword()).not.toBe(generateRolePassword());
  });
});

describe("tenantDatabaseUrl", () => {
  it("uses the tenant's role + URL-encoded password", () => {
    const url = tenantDatabaseUrl(TENANT, "abc/+=xyz");
    expect(url).toContain("tenant_3a2f1e4caaaabbbbccccdddd11112222");
    // `+`, `/`, `=` get percent-encoded
    expect(url).toContain("abc%2F%2B%3Dxyz");
  });

  it("preserves host + database name from the dashboard's DATABASE_URL", () => {
    const orig = process.env.DATABASE_URL;
    process.env.DATABASE_URL = "postgresql://u:p@somewhere:5433/mydb";
    try {
      const url = tenantDatabaseUrl(TENANT, "secret");
      expect(url).toContain("@somewhere:5433/mydb");
    } finally {
      if (orig === undefined) delete process.env.DATABASE_URL;
      else process.env.DATABASE_URL = orig;
    }
  });

  it("falls back to the docker-compose default when DATABASE_URL is unset", () => {
    const orig = process.env.DATABASE_URL;
    delete process.env.DATABASE_URL;
    try {
      const url = tenantDatabaseUrl(TENANT, "secret");
      expect(url).toContain("@postgres:5432/hypertrade");
    } finally {
      if (orig !== undefined) process.env.DATABASE_URL = orig;
    }
  });
});
