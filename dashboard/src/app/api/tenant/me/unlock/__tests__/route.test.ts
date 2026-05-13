/**
 * Tests for /api/tenant/me/unlock (security fix H-2 + audit M-4).
 *
 * Covers:
 *   - 11th attempt within window returns 429 (rate-limit gate before
 *     Argon2id derivation).
 *   - Each failed attempt writes a `passphrase.unlock-failed` audit row.
 *   - Successful unlock still works (no regression).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
  getSessionIdFromRequest: vi.fn(() => "session-id-123"),
}));

vi.mock("@/lib/rate-limit", () => ({
  checkRateLimit: vi.fn(),
}));

vi.mock("@/lib/audit-log", () => ({
  appendAuditLog: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/lib/crypto/passphrase", () => ({
  deriveKey: vi.fn(),
  verify: vi.fn(),
}));

vi.mock("@/lib/crypto/k-cache", () => ({
  cacheKey: vi.fn().mockResolvedValue(undefined),
  clearKey: vi.fn().mockResolvedValue(undefined),
}));

const updateChain = {
  set: vi.fn().mockReturnThis(),
  where: vi.fn().mockResolvedValue(undefined),
};
vi.mock("@/lib/db", () => ({
  db: {
    update: vi.fn(() => updateChain),
  },
  tenants: { id: "id" },
}));

import { appendAuditLog } from "@/lib/audit-log";
import { deriveKey, verify } from "@/lib/crypto/passphrase";
import { checkRateLimit } from "@/lib/rate-limit";
import { requireTenant } from "@/lib/tenant";

import { POST } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedRateLimit = vi.mocked(checkRateLimit);
const mockedAppendAuditLog = vi.mocked(appendAuditLog);
const mockedDeriveKey = vi.mocked(deriveKey);
const mockedVerify = vi.mocked(verify);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";

function tenant() {
  return {
    id: TENANT_ID,
    email: "test@example.com",
    displayName: "Test",
    isOperator: false,
    passphraseSalt: Buffer.from("salt-bytes-here-padded-to-16-len").subarray(0, 16),
    passphraseVerifier: Buffer.from("verifier-bytes-32-bytes-padded-here-bytes").subarray(0, 32),
  } as Awaited<ReturnType<typeof requireTenant>>;
}

function unlockReq(passphrase: string, ip = "5.6.7.8"): Request {
  return new Request("https://example.com/api/tenant/me/unlock", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-forwarded-for": ip,
    },
    body: JSON.stringify({ passphrase }),
  });
}

beforeEach(() => {
  mockedRequireTenant.mockResolvedValue(tenant());
  mockedRateLimit.mockResolvedValue({
    allowed: true,
    remaining: 9,
    resetInSeconds: 900,
  });
  mockedDeriveKey.mockResolvedValue(Buffer.alloc(32) as unknown as Awaited<ReturnType<typeof deriveKey>>);
  mockedVerify.mockReturnValue(false);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/tenant/me/unlock — H-2 + M-4", () => {
  it("returns 429 with Retry-After when rate-limit denies", async () => {
    mockedRateLimit.mockResolvedValueOnce({
      allowed: false,
      remaining: 0,
      resetInSeconds: 600,
    });
    const res = await POST(unlockReq("Letmein!2024"));
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("600");
    const body = await res.json();
    expect(body.error).toBe("rate-limited");
    expect(body.retry_after_seconds).toBe(600);
    // Argon2id MUST NOT have been called.
    expect(mockedDeriveKey).not.toHaveBeenCalled();
    // The denial itself should be audited.
    expect(mockedAppendAuditLog).toHaveBeenCalledWith(
      TENANT_ID,
      "tenant",
      "passphrase.unlock-rate-limited",
      expect.objectContaining({ ip: "5.6.7.8" }),
    );
  });

  it("each failed attempt writes a passphrase.unlock-failed audit row", async () => {
    mockedVerify.mockReturnValue(false);
    const res = await POST(unlockReq("wrong"));
    expect(res.status).toBe(401);
    expect(mockedAppendAuditLog).toHaveBeenCalledWith(
      TENANT_ID,
      "tenant",
      "passphrase.unlock-failed",
      expect.objectContaining({ ip: "5.6.7.8" }),
    );
  });

  it("11 failed unlocks → 11 audit rows + final 429", async () => {
    // Simulate 10 allowed + 1 denied. Each allowed-but-wrong attempt
    // should produce one audit-row write.
    mockedVerify.mockReturnValue(false);
    for (let i = 0; i < 10; i++) {
      mockedRateLimit.mockResolvedValueOnce({
        allowed: true,
        remaining: 9 - i,
        resetInSeconds: 900,
      });
      const res = await POST(unlockReq(`guess-${i}`));
      expect(res.status).toBe(401);
    }
    mockedRateLimit.mockResolvedValueOnce({
      allowed: false,
      remaining: 0,
      resetInSeconds: 900,
    });
    const denied = await POST(unlockReq("guess-11"));
    expect(denied.status).toBe(429);

    const failedAuditCalls = mockedAppendAuditLog.mock.calls.filter(
      (c) => c[2] === "passphrase.unlock-failed",
    );
    expect(failedAuditCalls).toHaveLength(10);
    const rateLimitedCalls = mockedAppendAuditLog.mock.calls.filter(
      (c) => c[2] === "passphrase.unlock-rate-limited",
    );
    expect(rateLimitedCalls).toHaveLength(1);
  });

  it("happy path unlocks and writes a passphrase.unlock audit row", async () => {
    mockedVerify.mockReturnValue(true);
    const res = await POST(unlockReq("correct-passphrase"));
    expect(res.status).toBe(200);
    expect(mockedAppendAuditLog).toHaveBeenCalledWith(
      TENANT_ID,
      "tenant",
      "passphrase.unlock",
    );
    // No failed-attempt audit row on success.
    const failedCalls = mockedAppendAuditLog.mock.calls.filter(
      (c) => c[2] === "passphrase.unlock-failed",
    );
    expect(failedCalls).toHaveLength(0);
  });
});
