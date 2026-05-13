/**
 * Unit tests for the tenant Postgres role helpers (multi-tenancy
 * Phase 5b). DB-touching functions (`provisionRole`, `dropRole`) are
 * covered by Phase 5c integration tests against a real Postgres —
 * here we test only the pure helpers and the URL builder.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

// In-memory Redis fake for getOrCreatePgRolePassword tests. Defined
// before the import below so the vi.mock factory can hand it back to
// the module under test without a hoisting hazard.
const fakeRedis = (() => {
  let store: Map<string, string> = new Map();
  return {
    reset: () => { store = new Map(); },
    get: vi.fn(async (k: string) => store.get(k) ?? null),
    set: vi.fn(async (k: string, v: string, mode?: string) => {
      if (mode === "NX") {
        if (store.has(k)) return null;
        store.set(k, v);
        return "OK";
      }
      store.set(k, v);
      return "OK";
    }),
    del: vi.fn(async (k: string) => (store.delete(k) ? 1 : 0)),
  };
})();

vi.mock("../redis", () => ({
  getRedisClient: () => fakeRedis,
}));

// db is imported at module load even for the pure helpers we're testing
// — provide a no-op stub so we don't hit a real Postgres.
vi.mock("../db", () => ({
  db: { execute: vi.fn() },
}));

import {
  forgetPgRolePassword,
  generateRolePassword,
  getOrCreatePgRolePassword,
  pgRolePasswordKey,
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

  it("throws on right-length but non-hex chars (PR #46 review)", () => {
    // Length 32 after dash-strip but contains 'g' (not a hex char).
    // Pre-fix this would have produced a Postgres-invalid identifier
    // and only blown up at provisionRole's regex re-check.
    const garbage = "ggggg444-aaaa-bbbb-cccc-dddd11112222";
    expect(() => roleNameForTenant(garbage)).toThrow(/32-hex UUID/);
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

  it("forces the postgresql+asyncpg scheme regardless of dashboard URL", () => {
    // The bot uses SQLAlchemy async via asyncpg — the dashboard's
    // libpq-style `postgresql://` scheme would crash it. PR #46
    // review fix: scheme is hardcoded.
    const orig = process.env.DATABASE_URL;
    process.env.DATABASE_URL = "postgresql://u:p@somewhere:5433/mydb";
    try {
      const url = tenantDatabaseUrl(TENANT, "secret");
      expect(url).toMatch(/^postgresql\+asyncpg:\/\//);
    } finally {
      if (orig === undefined) delete process.env.DATABASE_URL;
      else process.env.DATABASE_URL = orig;
    }
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

  it("preserves query string from the base URL (e.g. sslmode)", () => {
    // PR #46 review fix: `?sslmode=require` etc. on the dashboard
    // URL shouldn't be silently dropped when the tenant URL is built.
    const orig = process.env.DATABASE_URL;
    process.env.DATABASE_URL =
      "postgresql://u:p@somewhere:5432/mydb?sslmode=require&extra=1";
    try {
      const url = tenantDatabaseUrl(TENANT, "secret");
      expect(url).toContain("?sslmode=require&extra=1");
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
      // Still asyncpg scheme even on fallback
      expect(url).toMatch(/^postgresql\+asyncpg:\/\//);
    } finally {
      if (orig !== undefined) process.env.DATABASE_URL = orig;
    }
  });
});

describe("pgRolePasswordKey", () => {
  it("namespaces by tenant under tenant:<hex>:pg_role_pw", () => {
    expect(pgRolePasswordKey(TENANT)).toBe(
      "tenant:3a2f1e4caaaabbbbccccdddd11112222:pg_role_pw",
    );
  });
  it("rejects malformed tenant ids (would have leaked junk keys)", () => {
    expect(() => pgRolePasswordKey("not-a-uuid")).toThrow(/32-hex UUID/);
  });
});

describe("getOrCreatePgRolePassword (root-cause fix for 2026-05-13 incident)", () => {
  beforeEach(() => {
    fakeRedis.reset();
    vi.clearAllMocks();
  });

  it("returns the same password on repeated calls for the same tenant", async () => {
    // This is the regression test for the divergence incident: when
    // three bots for one tenant start sequentially, all three must
    // get the SAME DATABASE_URL or the older ones break on every
    // future DB query.
    const a = await getOrCreatePgRolePassword(TENANT);
    const b = await getOrCreatePgRolePassword(TENANT);
    const c = await getOrCreatePgRolePassword(TENANT);
    expect(a).toBe(b);
    expect(b).toBe(c);
    // First call SETNX wrote, subsequent calls hit the cache via GET.
    expect(fakeRedis.set).toHaveBeenCalledTimes(1);
  });

  it("writes through SETNX so concurrent calls converge to one value", async () => {
    // Simulate the SETNX race: two concurrent callers both miss the
    // GET, both attempt SET NX, only one wins, the loser reads the
    // winner's value.
    const [p1, p2, p3] = await Promise.all([
      getOrCreatePgRolePassword(TENANT),
      getOrCreatePgRolePassword(TENANT),
      getOrCreatePgRolePassword(TENANT),
    ]);
    expect(p1).toBe(p2);
    expect(p2).toBe(p3);
  });

  it("issues distinct passwords for distinct tenants", async () => {
    const otherTenant = "11111111-2222-3333-4444-555566667777";
    const a = await getOrCreatePgRolePassword(TENANT);
    const b = await getOrCreatePgRolePassword(otherTenant);
    expect(a).not.toBe(b);
  });

  it("regenerates after forgetPgRolePassword (rotation entry-point)", async () => {
    const before = await getOrCreatePgRolePassword(TENANT);
    await forgetPgRolePassword(TENANT);
    const after = await getOrCreatePgRolePassword(TENANT);
    // Cryptographically vanishingly small chance of collision; if
    // this ever flakes, generateRolePassword's randomness is broken.
    expect(after).not.toBe(before);
  });
});
