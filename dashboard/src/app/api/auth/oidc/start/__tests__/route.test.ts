/**
 * Tests for /api/auth/oidc/start (security fix H-2).
 *
 * Covers:
 *   - Rate-limit gate: when checkRateLimit denies, return 429 with
 *     Retry-After header and DO NOT call the IdP / mint state cookie.
 *   - When allowed, the existing 307-to-IdP redirect is preserved.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/rate-limit", () => ({
  checkRateLimit: vi.fn(),
}));

vi.mock("@/lib/oidc", () => ({
  getOidcConfig: vi.fn(),
  encodeStateBundle: vi.fn(() => "encoded-state-bundle"),
  STATE_COOKIE: "oidc_state",
  STATE_COOKIE_OPTIONS: { httpOnly: true, path: "/" },
  safeNext: vi.fn((s: string) => s),
  resolveRedirectUri: vi.fn(() => "https://example.com/api/auth/oidc/callback"),
}));

vi.mock("openid-client", () => ({
  randomPKCECodeVerifier: vi.fn(() => "verifier"),
  calculatePKCECodeChallenge: vi.fn().mockResolvedValue("challenge"),
  randomState: vi.fn(() => "state-string"),
  buildAuthorizationUrl: vi.fn(
    () => new URL("https://idp.example/authorize?state=state-string"),
  ),
}));

import * as client from "openid-client";

import { getOidcConfig } from "@/lib/oidc";
import { checkRateLimit } from "@/lib/rate-limit";

import { GET } from "../route";

const mockedRateLimit = vi.mocked(checkRateLimit);
const mockedGetOidcConfig = vi.mocked(getOidcConfig);
const mockedBuildAuthUrl = vi.mocked(client.buildAuthorizationUrl);

function startReq(ip = "9.9.9.9"): Request {
  return new Request("https://example.com/api/auth/oidc/start", {
    method: "GET",
    headers: { "x-forwarded-for": ip },
  });
}

beforeEach(() => {
  mockedRateLimit.mockResolvedValue({
    allowed: true,
    remaining: 59,
    resetInSeconds: 60,
  });
  mockedGetOidcConfig.mockResolvedValue({
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    config: {} as any,
    cfg: {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      oidc_scopes: "openid profile email",
    } as any,
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/auth/oidc/start — H-2", () => {
  it("returns 429 with Retry-After when rate-limited", async () => {
    mockedRateLimit.mockResolvedValueOnce({
      allowed: false,
      remaining: 0,
      resetInSeconds: 45,
    });
    const res = await GET(startReq());
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("45");
    const body = await res.json();
    expect(body.error).toBe("rate-limited");
    expect(body.retry_after_seconds).toBe(45);
    // No IdP work should have happened.
    expect(mockedGetOidcConfig).not.toHaveBeenCalled();
    expect(mockedBuildAuthUrl).not.toHaveBeenCalled();
  });

  it("denies after the per-IP limit (60/min) is reached", async () => {
    // Walk the helper: 60 allowed redirects, then 1 denied.
    // We don't actually fire 60 — just verify the route honors the
    // boolean on each call. The numeric limit is enforced by
    // checkRateLimit (covered by its own tests).
    mockedRateLimit
      .mockResolvedValueOnce({ allowed: true, remaining: 0, resetInSeconds: 60 });
    let res = await GET(startReq());
    expect(res.status).toBe(307); // NextResponse.redirect

    mockedRateLimit
      .mockResolvedValueOnce({ allowed: false, remaining: 0, resetInSeconds: 60 });
    res = await GET(startReq());
    expect(res.status).toBe(429);
  });

  it("happy path 307s to the IdP and sets the state cookie", async () => {
    const res = await GET(startReq());
    expect(res.status).toBe(307);
    expect(res.headers.get("location")).toContain("idp.example/authorize");
    expect(res.headers.get("set-cookie")).toContain("oidc_state=");
  });
});
