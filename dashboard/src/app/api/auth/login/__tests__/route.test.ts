/**
 * Tests for /api/auth/login (security fix H-2).
 *
 * Covers:
 *   - Rate-limit gate: 11th attempt within window returns 429.
 *   - Constant-time path: bcrypt.verify is ALWAYS called, even on
 *     unknown username, so an attacker can't enumerate the basic_user
 *     via timing.
 *   - User-not-found and wrong-password both return the same generic
 *     401 error code.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@node-rs/bcrypt", () => ({
  // PR #92 Copilot review fix: DUMMY_HASH is now a hardcoded
  // string constant in route.ts (no `hashSync` at module load),
  // so the test mock only needs `verify`.
  verify: vi.fn(),
}));

// Must match the constant in `route.ts` exactly. Used to assert that
// user-not-found goes through the dummy verify path, not the stored
// hash path.
const DUMMY_HASH =
  "$2y$12$ZkgAhco9SGGGbpEEVfxrgOb6BPW73tCuEMAbrPaC4QunY/iOBqDaa";

vi.mock("@/lib/rate-limit", () => ({
  checkRateLimit: vi.fn(),
}));

vi.mock("@/lib/auth", () => ({
  fetchAuthConfig: vi.fn(),
  getSessionSecret: vi.fn(),
  signSession: vi.fn(() => "signed-cookie-value"),
  newSessionPayload: vi.fn((sub: string) => ({ sub, iat: 0, exp: 0 })),
  COOKIE_OPTIONS: { name: "hypertrade_session", maxAge: 3600 },
}));

vi.mock("@/lib/auth-config", () => ({
  getAuthConfig: vi.fn(),
}));

import { verify as bcryptVerify } from "@node-rs/bcrypt";
import { fetchAuthConfig, getSessionSecret } from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";
import { checkRateLimit } from "@/lib/rate-limit";

import { POST } from "../route";

const mockedBcryptVerify = vi.mocked(bcryptVerify);
const mockedRateLimit = vi.mocked(checkRateLimit);
const mockedFetchCfg = vi.mocked(fetchAuthConfig);
const mockedGetSessionSecret = vi.mocked(getSessionSecret);
const mockedGetAuthConfig = vi.mocked(getAuthConfig);

function loginReq(
  body: { username?: string; password?: string },
  ip = "1.2.3.4",
): Request {
  return new Request("https://example.com/api/auth/login", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-forwarded-for": ip,
    },
    body: JSON.stringify(body),
  });
}

beforeEach(() => {
  // Default: rate-limit allows. Override per test for denial cases.
  mockedRateLimit.mockResolvedValue({
    allowed: true,
    remaining: 9,
    resetInSeconds: 900,
  });
  mockedFetchCfg.mockResolvedValue({
    mode: "basic",
    basic_user_set: true,
    oidc_issuer: "",
    oidc_client_id: "",
    oidc_scopes: "",
  });
  mockedGetAuthConfig.mockResolvedValue({
    basic_user: "alice",
    basic_hash: "$2b$12$real.stored.hash.for.alice.password.value",
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any);
  mockedGetSessionSecret.mockResolvedValue("test-secret");
  mockedBcryptVerify.mockResolvedValue(false);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/auth/login — H-2 hardening", () => {
  it("returns 400 on missing credentials", async () => {
    const res = await POST(loginReq({}));
    expect(res.status).toBe(400);
    expect(mockedRateLimit).not.toHaveBeenCalled();
  });

  it("rejects with 429 when IP rate-limit denies", async () => {
    mockedRateLimit.mockResolvedValueOnce({
      allowed: false,
      remaining: 0,
      resetInSeconds: 600,
    });
    const res = await POST(loginReq({ username: "alice", password: "x" }));
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("600");
    const body = await res.json();
    expect(body.error).toBe("rate-limited");
    expect(body.retry_after_seconds).toBe(600);
    // Must short-circuit BEFORE auth-config fetch / bcrypt.
    expect(mockedFetchCfg).not.toHaveBeenCalled();
    expect(mockedBcryptVerify).not.toHaveBeenCalled();
  });

  it("rejects with 429 when per-username rate-limit denies", async () => {
    mockedRateLimit
      .mockResolvedValueOnce({ allowed: true, remaining: 9, resetInSeconds: 900 }) // ip
      .mockResolvedValueOnce({ allowed: false, remaining: 0, resetInSeconds: 700 }); // user
    const res = await POST(loginReq({ username: "alice", password: "x" }));
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("700");
    expect(mockedBcryptVerify).not.toHaveBeenCalled();
  });

  it("11th attempt within window is denied (1st-10th allowed)", async () => {
    // Walk the helper through 10 allowed + 1 denied. Real logic
    // lives in checkRateLimit (covered by its own tests); here
    // we just confirm the route honors the boolean flag.
    for (let i = 0; i < 10; i++) {
      mockedRateLimit
        .mockResolvedValueOnce({ allowed: true, remaining: 9 - i, resetInSeconds: 900 })
        .mockResolvedValueOnce({ allowed: true, remaining: 9 - i, resetInSeconds: 900 });
      const res = await POST(loginReq({ username: "alice", password: "wrong" }));
      expect(res.status).toBe(401);
    }
    mockedRateLimit.mockResolvedValueOnce({
      allowed: false,
      remaining: 0,
      resetInSeconds: 900,
    });
    const res = await POST(loginReq({ username: "alice", password: "wrong" }));
    expect(res.status).toBe(429);
  });

  it("bcrypt.verify runs on user-not-found (constant-time vs user-found-bad-pw)", async () => {
    // Unknown username — must still call bcryptVerify (against the
    // dummy hash) to flatten the timing oracle.
    mockedBcryptVerify.mockResolvedValue(false);
    const res = await POST(loginReq({ username: "nonexistent", password: "anything" }));
    expect(res.status).toBe(401);
    expect(mockedBcryptVerify).toHaveBeenCalledTimes(1);
    const [, hashArg] = mockedBcryptVerify.mock.calls[0];
    // Crucially — the hash passed must NOT be the real stored hash;
    // it must be the hardcoded DUMMY_HASH from route.ts.
    expect(hashArg).toBe(DUMMY_HASH);
  });

  it("bcrypt.verify runs on user-found-wrong-password", async () => {
    mockedBcryptVerify.mockResolvedValue(false);
    const res = await POST(loginReq({ username: "alice", password: "wrong" }));
    expect(res.status).toBe(401);
    expect(mockedBcryptVerify).toHaveBeenCalledTimes(1);
    const [, hashArg] = mockedBcryptVerify.mock.calls[0];
    expect(hashArg).toBe("$2b$12$real.stored.hash.for.alice.password.value");
  });

  it("user-not-found and wrong-password return the same error code", async () => {
    mockedBcryptVerify.mockResolvedValue(false);
    const r1 = await POST(loginReq({ username: "alice", password: "wrong" }));
    const r2 = await POST(loginReq({ username: "nonexistent", password: "wrong" }));
    expect(r1.status).toBe(r2.status);
    const b1 = await r1.json();
    const b2 = await r2.json();
    expect(b1.error).toBe(b2.error);
    expect(b1.error).toBe("invalid-credentials");
  });

  it("happy path issues a session cookie", async () => {
    mockedBcryptVerify.mockResolvedValueOnce(true);
    const res = await POST(loginReq({ username: "alice", password: "right" }));
    expect(res.status).toBe(200);
    expect(res.headers.get("set-cookie")).toContain("hypertrade_session=");
  });

  it("preserves the bot-unreachable error code on Redis failure", async () => {
    mockedFetchCfg.mockResolvedValueOnce(null);
    const res = await POST(loginReq({ username: "alice", password: "x" }));
    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.error).toBe("bot-unreachable");
  });
});
