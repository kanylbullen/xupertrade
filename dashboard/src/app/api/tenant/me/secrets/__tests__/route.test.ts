/**
 * Tests for PUT /api/tenant/me/secrets/[key].
 *
 * Security audit C-1: PUT is gated by an allowlist of env-var names
 * a tenant may set. DELETE keeps the broader regex so tenants can
 * clean up legacy non-allowlisted keys without an operator-side
 * migration.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
  requireUnlockedKey: vi.fn(),
}));

vi.mock("@/lib/crypto/secrets", () => ({
  encryptSecret: vi.fn(() => ({
    ciphertext: Buffer.from("ct"),
    nonce: Buffer.from("nonce"),
  })),
}));

const insertChain = {
  values: vi.fn().mockReturnThis(),
  onConflictDoUpdate: vi.fn().mockResolvedValue(undefined),
};
vi.mock("@/lib/db", () => ({
  db: {
    insert: vi.fn(() => insertChain),
  },
  tenantSecrets: {
    tenantId: "tenantId",
    key: "key",
  },
}));

import { db } from "@/lib/db";
import { requireTenant, requireUnlockedKey } from "@/lib/tenant";

import { PUT } from "../[key]/route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedRequireUnlockedKey = vi.mocked(requireUnlockedKey);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";

function makeTenant() {
  return {
    id: TENANT_ID,
    email: "test@example.com",
    displayName: "Test User",
    isOperator: false,
    passphraseSalt: Buffer.alloc(16, 1),
    passphraseVerifier: Buffer.alloc(32, 7),
  } as Awaited<ReturnType<typeof requireTenant>>;
}

function makeReq(value: string, extra?: Record<string, unknown>): Request {
  return new Request("https://example.com/api/tenant/me/secrets/X", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value, ...(extra ?? {}) }),
  });
}

function makeCtx(key: string) {
  return { params: Promise.resolve({ key }) };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("PUT /api/tenant/me/secrets/[key] — allowlist (C-1)", () => {
  it.each([
    "HYPERLIQUID_PRIVATE_KEY",
    "HYPERLIQUID_ACCOUNT_ADDRESS",
    "HYPERLIQUID_MAINNET_PRIVATE_KEY",
    "HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "VAULT_TRACKING_ADDRESS",
  ])("accepts allowlisted key %s and writes it", async (key) => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32, 9));

    const res = await PUT(makeReq("some-value"), makeCtx(key));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({ key, set: true });
    expect(db.insert).toHaveBeenCalledOnce();
  });

  it.each([
    "MAINNET_ENABLED_STRATEGIES",
    "MAX_TOTAL_EXPOSURE_USD",
    "SIGNAL_SIZE_MAX_MULTIPLIER",
    "TAKER_FEE_RATE",
    "TRADE_RATE_ALARM_ENABLED",
    "HL_ORDER_TIMEOUT_SECONDS",
    "API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "TELEGRAM_EVENTS",
    "RANDOM_FUTURE_KEY",
  ])("rejects disallowed key %s with 400 and does not write", async (key) => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());

    const res = await PUT(makeReq("some-value"), makeCtx(key));
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toMatch(/not a tenant-settable secret/);
    // Critical: row must NOT be written.
    expect(db.insert).not.toHaveBeenCalled();
    // Allowlist gate runs BEFORE we ask for the unlock key.
    expect(mockedRequireUnlockedKey).not.toHaveBeenCalled();
  });

  it("rejects syntactically invalid keys with the regex error (not the allowlist error)", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());

    const res = await PUT(makeReq("v"), makeCtx("lowercase_key"));
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toMatch(/\[A-Z0-9_\]\{1,64\}/);
    expect(db.insert).not.toHaveBeenCalled();
  });
});

describe("PUT expiresAt — HL key rotation reminders", () => {
  it("stores expiresAt for HYPERLIQUID_PRIVATE_KEY and roundtrips it", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32, 9));

    const res = await PUT(
      makeReq("0x" + "a".repeat(64), { expiresAt: "2027-01-15" }),
      makeCtx("HYPERLIQUID_PRIVATE_KEY"),
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.expiresAt).toBe("2027-01-15T00:00:00.000Z");

    const valuesArg = insertChain.values.mock.calls[0][0];
    expect(valuesArg.expiresAt).toBeInstanceOf(Date);
    expect((valuesArg.expiresAt as Date).toISOString()).toBe(
      "2027-01-15T00:00:00.000Z",
    );
  });

  it("accepts empty string as null (clears expiry)", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32, 9));

    const res = await PUT(
      makeReq("0x" + "b".repeat(64), { expiresAt: "" }),
      makeCtx("HYPERLIQUID_MAINNET_PRIVATE_KEY"),
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.expiresAt).toBeNull();
    const valuesArg = insertChain.values.mock.calls[0][0];
    expect(valuesArg.expiresAt).toBeNull();
  });

  it("400s on invalid date format", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32, 9));

    const res = await PUT(
      makeReq("v", { expiresAt: "not-a-date" }),
      makeCtx("HYPERLIQUID_PRIVATE_KEY"),
    );
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toMatch(/expiresAt/);
  });

  it("silently drops expiresAt for non-HL keys", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32, 9));

    const res = await PUT(
      makeReq("token", { expiresAt: "2027-01-15" }),
      makeCtx("TELEGRAM_BOT_TOKEN"),
    );
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.expiresAt).toBeUndefined();
    const valuesArg = insertChain.values.mock.calls[0][0];
    expect("expiresAt" in valuesArg).toBe(false);
  });
});
