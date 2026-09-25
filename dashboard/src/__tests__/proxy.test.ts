/**
 * Tests for the auth gate in `src/proxy.ts` — the `locked` branch.
 *
 * `locked` is what `resolveMode` answers when the stored auth mode is
 * gone (flushed Redis, removed volume) on an installation that has
 * tenants. It is the opposite of `disabled`: nothing renders, every
 * gated path goes to /login with `error=auth-locked`. Before #168 the
 * same situation resolved to `disabled` and served the operator's data
 * to anyone; this branch is what stands in the way, and it had no test.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/auth", () => ({
  SESSION_COOKIE: "hypertrade_session",
  fetchAuthConfig: vi.fn(),
  getSessionSecret: vi.fn(),
  verifySession: vi.fn(),
}));

vi.mock("@/lib/session-store", () => ({
  isSessionRevoked: vi.fn(),
}));

import {
  fetchAuthConfig,
  getSessionSecret,
  verifySession,
  type AuthConfig,
} from "@/lib/auth";
import { isSessionRevoked } from "@/lib/session-store";

import { proxy } from "../proxy";

const mockedFetchCfg = vi.mocked(fetchAuthConfig);
const mockedSecret = vi.mocked(getSessionSecret);
const mockedVerify = vi.mocked(verifySession);
const mockedRevoked = vi.mocked(isSessionRevoked);

const ORIG_ENV = { ...process.env };

function cfg(mode: AuthConfig["mode"]): AuthConfig {
  return {
    mode,
    basic_user_set: true,
    oidc_issuer: "",
    oidc_client_id: "",
    oidc_scopes: "openid profile email",
  };
}

function req(path: string, cookie?: string): NextRequest {
  return new NextRequest(`https://dash.example.test${path}`, {
    headers: cookie ? { cookie: `hypertrade_session=${cookie}` } : {},
  });
}

/** NextResponse.next() marks pass-through with this header. */
function passedThrough(res: Response): boolean {
  return res.headers.get("x-middleware-next") === "1";
}

beforeEach(() => {
  delete process.env.PUBLIC_URL;
  delete process.env.DASHBOARD_URL;
  // A valid, unrevoked session is available — locked must win anyway.
  mockedSecret.mockResolvedValue("secret");
  mockedVerify.mockReturnValue({ sub: "operator", iat: 0, exp: 9e9 });
  mockedRevoked.mockResolvedValue(false);
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.clearAllMocks();
});

describe("proxy — locked", () => {
  it.each(["/", "/trades", "/options", "/api/auth/configure", "/admin"])(
    "redirects %s to /login?error=auth-locked",
    async (path) => {
      mockedFetchCfg.mockResolvedValue(cfg("locked"));

      const res = await proxy(req(path));

      expect(res.status).toBe(307);
      const loc = new URL(res.headers.get("location")!);
      expect(loc.pathname).toBe("/login");
      expect(loc.searchParams.get("error")).toBe("auth-locked");
      expect(loc.searchParams.get("next")).toBe(path);
    },
  );

  it("withholds pages even from a request with a valid session", async () => {
    // Locked is decided before the cookie is looked at: the resolver
    // can't say how this install authenticates, so no session is
    // evidence of anything.
    mockedFetchCfg.mockResolvedValue(cfg("locked"));

    const res = await proxy(req("/options", "a-valid-looking-cookie"));

    expect(res.status).toBe(307);
    expect(new URL(res.headers.get("location")!).searchParams.get("error")).toBe(
      "auth-locked",
    );
    expect(mockedVerify).not.toHaveBeenCalled();
  });

  it("builds the redirect on PUBLIC_URL, not the container's own host", async () => {
    process.env.PUBLIC_URL = "https://public.example.test/";
    mockedFetchCfg.mockResolvedValue(cfg("locked"));

    const res = await proxy(req("/trades"));

    const loc = new URL(res.headers.get("location")!);
    expect(loc.origin).toBe("https://public.example.test");
    expect(loc.pathname).toBe("/login");
  });

  it.each([
    "/login",
    "/api/auth/config",
    "/api/auth/login",
    "/api/healthz",
    "/api/version",
  ])(
    "still lets public path %s through, so the notice can render",
    async (path) => {
      mockedFetchCfg.mockResolvedValue(cfg("locked"));

      const res = await proxy(req(path));

      expect(passedThrough(res)).toBe(true);
      expect(mockedFetchCfg).not.toHaveBeenCalled();
    },
  );
});

describe("proxy — contrast with the neighbouring modes", () => {
  it("disabled lets a gated path through with no session", async () => {
    mockedFetchCfg.mockResolvedValue(cfg("disabled"));
    const res = await proxy(req("/trades"));
    expect(passedThrough(res)).toBe(true);
  });

  it("basic lets a valid session through", async () => {
    mockedFetchCfg.mockResolvedValue(cfg("basic"));
    const res = await proxy(req("/trades", "cookie"));
    expect(passedThrough(res)).toBe(true);
  });

  it("an unreadable auth config fails closed with its own code", async () => {
    mockedFetchCfg.mockResolvedValue(null);
    const res = await proxy(req("/trades"));
    expect(res.status).toBe(307);
    expect(new URL(res.headers.get("location")!).searchParams.get("error")).toBe(
      "bot-unreachable",
    );
  });
});
