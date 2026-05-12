/**
 * Tests for GET /api/tenant/me — verifies the passphraseSet/unlocked
 * derivation that the credentials wizard branches on.
 *
 * Mocks the tenant resolver + k-cache so we don't need a live DB or
 * Redis. The route logic is pure derivation from the tenant row +
 * one Redis GET, so this fully exercises it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
  getSessionIdFromRequest: vi.fn(),
}));
vi.mock("@/lib/crypto/k-cache", () => ({
  loadKey: vi.fn(),
}));

import { loadKey } from "@/lib/crypto/k-cache";
import { getSessionIdFromRequest, requireTenant } from "@/lib/tenant";

import { GET } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedSessionId = vi.mocked(getSessionIdFromRequest);
const mockedLoadKey = vi.mocked(loadKey);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";
const SESSION_ID = "abcdef0123456789abcdef0123456789";

function makeTenant(overrides: Partial<{
  passphraseVerifier: Buffer | null;
  isOperator: boolean;
}> = {}) {
  return {
    id: TENANT_ID,
    email: "test@example.com",
    displayName: "Test User",
    isOperator: overrides.isOperator ?? false,
    passphraseSalt: overrides.passphraseVerifier ? Buffer.alloc(16, 1) : null,
    passphraseVerifier:
      overrides.passphraseVerifier === undefined
        ? null
        : overrides.passphraseVerifier,
  } as Awaited<ReturnType<typeof requireTenant>>;
}

function makeReq(): Request {
  return new Request("https://example.com/api/tenant/me");
}

afterEach(() => {
  mockedRequireTenant.mockReset();
  mockedSessionId.mockReset();
  mockedLoadKey.mockReset();
});

describe("GET /api/tenant/me", () => {
  it("returns passphraseSet=false, unlocked=false for a fresh tenant", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());

    const res = await GET(makeReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toMatchObject({
      id: TENANT_ID,
      passphraseSet: false,
      unlocked: false,
    });
    // Should never call loadKey when no passphrase is set — wastes a
    // Redis round-trip and could hide bugs in the unlocked-derivation.
    expect(mockedLoadKey).not.toHaveBeenCalled();
  });

  it("returns passphraseSet=true, unlocked=false when passphrase set but K not cached", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ passphraseVerifier: Buffer.alloc(32, 7) }),
    );
    mockedSessionId.mockReturnValueOnce(SESSION_ID);
    mockedLoadKey.mockResolvedValueOnce(null);

    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.passphraseSet).toBe(true);
    expect(body.unlocked).toBe(false);
    expect(mockedLoadKey).toHaveBeenCalledWith(TENANT_ID, SESSION_ID);
  });

  it("returns unlocked=true when K is cached", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ passphraseVerifier: Buffer.alloc(32, 7) }),
    );
    mockedSessionId.mockReturnValueOnce(SESSION_ID);
    mockedLoadKey.mockResolvedValueOnce(Buffer.alloc(32, 9));

    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.passphraseSet).toBe(true);
    expect(body.unlocked).toBe(true);
  });

  it("treats missing session-id as unlocked=false even when passphrase is set", async () => {
    // Defensive: requireTenant succeeded so there IS a valid session,
    // but getSessionIdFromRequest returning null would be a bug. We
    // surface it as locked rather than crashing.
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ passphraseVerifier: Buffer.alloc(32, 7) }),
    );
    mockedSessionId.mockReturnValueOnce(null);

    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.unlocked).toBe(false);
    expect(mockedLoadKey).not.toHaveBeenCalled();
  });

  it("propagates 401 from requireTenant", async () => {
    mockedRequireTenant.mockRejectedValueOnce(
      new Response(JSON.stringify({ error: "not authenticated" }), {
        status: 401,
      }),
    );

    const res = await GET(makeReq());
    expect(res.status).toBe(401);
  });

  it("includes isOperator flag in response", async () => {
    mockedRequireTenant.mockResolvedValueOnce(
      makeTenant({ isOperator: true }),
    );

    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.isOperator).toBe(true);
  });
});
