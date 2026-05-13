/**
 * Tests for session revocation (security audit H-3).
 *
 * Covers the three layers added in fix/sec-h3-session-revocation:
 *   - markSessionRevoked + isSessionRevoked round-trip via Redis stub
 *   - fail-closed on Redis error in isSessionRevoked
 *   - K-cache default TTL is now 24h (was 7 days)
 *   - logout endpoint marks the cookie revoked AND evicts the
 *     k-cache entry for the resolved tenant + session, even when
 *     individual layers fail
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isSessionRevoked, markSessionRevoked } from "../session-store";
import { DEFAULT_TTL_SECONDS } from "../crypto/k-cache";

type RedisStubOptions = {
  getValue?: string | null;
  getThrows?: boolean;
};

function makeRedisStub(opts: RedisStubOptions = {}) {
  const get = opts.getThrows
    ? vi.fn().mockRejectedValue(new Error("redis down"))
    : vi.fn().mockResolvedValue(opts.getValue ?? null);
  const set = vi.fn().mockResolvedValue("OK");
  const del = vi.fn().mockResolvedValue(1);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { client: { get, set, del } as any, get, set, del };
}

afterEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
});

describe("session-store", () => {
  it("marks a session revoked under the sha256-keyed Redis slot", async () => {
    const { client, set } = makeRedisStub();
    await markSessionRevoked("payload.signature", client);
    expect(set).toHaveBeenCalledTimes(1);
    const [key, value, mode, ttl] = set.mock.calls[0];
    expect(key).toMatch(/^session:revoked:[a-f0-9]{64}$/);
    expect(value).toBe("1");
    expect(mode).toBe("EX");
    expect(typeof ttl).toBe("number");
    // 8-day TTL safely covers the 7-day session exp.
    expect(ttl).toBe(60 * 60 * 24 * 8);
  });

  it("no-ops on empty cookie (markSessionRevoked)", async () => {
    const { client, set } = makeRedisStub();
    await markSessionRevoked("", client);
    expect(set).not.toHaveBeenCalled();
  });

  it("isSessionRevoked returns true when Redis has the key", async () => {
    const { client } = makeRedisStub({ getValue: "1" });
    expect(await isSessionRevoked("cookie-value", client)).toBe(true);
  });

  it("isSessionRevoked returns false when Redis has no key", async () => {
    const { client } = makeRedisStub({ getValue: null });
    expect(await isSessionRevoked("cookie-value", client)).toBe(false);
  });

  it("isSessionRevoked fails CLOSED on Redis error (returns true)", async () => {
    const { client } = makeRedisStub({ getThrows: true });
    expect(await isSessionRevoked("cookie-value", client)).toBe(true);
  });

  it("isSessionRevoked short-circuits on empty cookie without hitting Redis", async () => {
    const { client, get } = makeRedisStub();
    expect(await isSessionRevoked("", client)).toBe(false);
    expect(get).not.toHaveBeenCalled();
  });

  it("uses the same Redis key for the same cookie (deterministic)", async () => {
    const { client: a, set: setA } = makeRedisStub();
    const { client: b, get: getB } = makeRedisStub({ getValue: "1" });
    await markSessionRevoked("the-same-cookie", a);
    await isSessionRevoked("the-same-cookie", b);
    expect(setA.mock.calls[0][0]).toBe(getB.mock.calls[0][0]);
  });
});

describe("k-cache default TTL", () => {
  it("is 24 hours (lowered from 7 days per audit H-3)", () => {
    expect(DEFAULT_TTL_SECONDS).toBe(60 * 60 * 24);
  });
});

/**
 * Logout endpoint integration. Mocks each layer's external dependency
 * and asserts the route invokes them in the right order with the
 * right inputs. Uses vi.mock at module-load time so the route picks
 * up our stubs.
 */
describe("/api/auth/logout", () => {
  const markRevoked = vi.fn().mockResolvedValue(undefined);
  const clearK = vi.fn().mockResolvedValue(undefined);
  const verify = vi.fn();
  const getSecret = vi.fn().mockResolvedValue("test-secret");
  const dbSelectChain = {
    from: vi.fn().mockReturnThis(),
    where: vi.fn().mockReturnThis(),
    limit: vi.fn().mockResolvedValue([{ id: "tenant-uuid-123" }]),
  };
  const dbSelect = vi.fn(() => dbSelectChain);

  beforeEach(() => {
    vi.resetModules();
    markRevoked.mockClear();
    clearK.mockClear();
    verify.mockReset();
    getSecret.mockClear();
    dbSelectChain.from.mockClear();
    dbSelectChain.where.mockClear();
    dbSelectChain.limit.mockClear().mockResolvedValue([
      { id: "tenant-uuid-123" },
    ]);
    dbSelect.mockClear();
  });

  function setupMocks() {
    vi.doMock("../session-store", () => ({
      markSessionRevoked: markRevoked,
      isSessionRevoked: vi.fn().mockResolvedValue(false),
    }));
    vi.doMock("../crypto/k-cache", () => ({
      clearKey: clearK,
      DEFAULT_TTL_SECONDS: 60 * 60 * 24,
    }));
    vi.doMock("../auth", async () => {
      const actual = await vi.importActual<typeof import("../auth")>("../auth");
      return {
        ...actual,
        getSessionSecret: getSecret,
        verifySession: verify,
      };
    });
    vi.doMock("../db", () => ({
      db: { select: dbSelect },
      tenants: { id: "id", authentikSub: "authentikSub" },
    }));
  }

  it("revokes session + evicts k-cache + clears cookie on POST", async () => {
    verify.mockReturnValue({ sub: "user@example.com", iat: 0, exp: 9_999_999_999 });
    setupMocks();

    const { POST } = await import("../../app/api/auth/logout/route");
    const req = new Request("http://localhost/api/auth/logout", {
      method: "POST",
      headers: { cookie: "hypertrade_session=abc.def" },
    });
    const res = await POST(req);

    expect(res.status).toBe(200);
    expect(markRevoked).toHaveBeenCalledWith("abc.def");
    expect(clearK).toHaveBeenCalledWith(
      "tenant-uuid-123",
      // sessionId = sha256("abc.def").slice(0, 32) — recompute to match
      expect.stringMatching(/^[a-f0-9]{32}$/),
    );
    // Cookie cleared.
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toMatch(/hypertrade_session=;/);
    expect(setCookie).toMatch(/Max-Age=0/i);
  });

  it("logout never errors when there is no cookie", async () => {
    setupMocks();
    const { POST } = await import("../../app/api/auth/logout/route");
    const req = new Request("http://localhost/api/auth/logout", {
      method: "POST",
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    expect(markRevoked).not.toHaveBeenCalled();
    expect(clearK).not.toHaveBeenCalled();
  });

  it("logout still clears cookie when revocation throws", async () => {
    markRevoked.mockRejectedValueOnce(new Error("redis down"));
    verify.mockReturnValue(null);
    setupMocks();
    const { POST } = await import("../../app/api/auth/logout/route");
    const req = new Request("http://localhost/api/auth/logout", {
      method: "POST",
      headers: { cookie: "hypertrade_session=oops" },
    });
    const res = await POST(req);
    expect(res.status).toBe(200);
    expect((res.headers.get("set-cookie") ?? "")).toMatch(/Max-Age=0/i);
  });

  it("logout skips k-cache eviction when cookie HMAC is invalid", async () => {
    verify.mockReturnValue(null);
    setupMocks();
    const { POST } = await import("../../app/api/auth/logout/route");
    const req = new Request("http://localhost/api/auth/logout", {
      method: "POST",
      headers: { cookie: "hypertrade_session=garbage" },
    });
    await POST(req);
    // Revocation always runs; k-cache eviction needs a valid payload.
    expect(markRevoked).toHaveBeenCalledOnce();
    expect(clearK).not.toHaveBeenCalled();
  });
});
