/**
 * Tests for /api/auth/oidc/callback.
 *
 * The locked branch is the reason this file exists: `resolveMode`
 * answers "locked" when the stored auth mode is gone on an install with
 * tenants, and basic login refuses to mint a session then. The callback
 * used to check only issuer, client id and secret, so a full OIDC round
 * trip minted a session cookie mid-lock — one that outlived the lock.
 *
 * `lib/oidc` is NOT mocked here: the real `getOidcConfig` runs against
 * a mocked auth config and a mocked `openid-client`, so "the IdP is
 * never contacted" is asserted on discovery and the token exchange
 * themselves rather than on a stand-in.
 *
 * Covers:
 *   - Locked: redirect to /login?error=auth-locked on the PUBLIC_URL
 *     host, no session cookie, the state cookie cleared, no discovery,
 *     no token exchange, no session-secret read.
 *   - Not locked: the happy path and every existing error path behave
 *     as before.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("openid-client", () => ({
  discovery: vi.fn(),
  authorizationCodeGrant: vi.fn(),
}));

vi.mock("@/lib/auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/auth")>();
  return {
    ...actual,
    fetchAuthConfig: vi.fn(),
    getSessionSecret: vi.fn(),
  };
});

// `getOidcConfig` reads the client secret through this.
vi.mock("@/lib/auth-config", () => ({
  getAuthConfig: vi.fn(),
}));

import * as client from "openid-client";

import {
  fetchAuthConfig,
  getSessionSecret,
  SESSION_COOKIE,
  type AuthConfig,
} from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";
import { encodeStateBundle, STATE_COOKIE } from "@/lib/oidc";

import { GET } from "../route";

const mockedDiscovery = vi.mocked(client.discovery);
const mockedCodeGrant = vi.mocked(client.authorizationCodeGrant);
const mockedFetchCfg = vi.mocked(fetchAuthConfig);
const mockedSecret = vi.mocked(getSessionSecret);
const mockedGetAuthConfig = vi.mocked(getAuthConfig);

const ORIG_ENV = { ...process.env };
const PUBLIC = "https://public.example.test";
const DISCOVERED = { discovered: true } as unknown as client.Configuration;

function cfg(mode: AuthConfig["mode"]): AuthConfig {
  return {
    mode,
    basic_user_set: false,
    // Present on purpose: locked must win even with OIDC fully set up.
    oidc_issuer: "https://idp.example.test",
    oidc_client_id: "client-id",
    oidc_scopes: "openid profile email",
  };
}

const STATE_BUNDLE = encodeStateBundle({
  code_verifier: "verifier",
  state: "state-string",
  next: "/trades",
});

/** The callback as the IdP sends it: on the container's own hostname
 *  (what `req.url` carries in production), with code + state. */
function callbackReq(stateCookie: string | null = STATE_BUNDLE): Request {
  return new Request(
    "http://container-1234:3000/api/auth/oidc/callback?code=auth-code&state=state-string",
    {
      method: "GET",
      headers: stateCookie ? { cookie: `${STATE_COOKIE}=${stateCookie}` } : {},
    },
  );
}

function location(res: Response): URL {
  return new URL(res.headers.get("location")!);
}

function setCookie(res: Response, name: string): string | undefined {
  return res.headers.getSetCookie().find((c) => c.startsWith(`${name}=`));
}

beforeEach(() => {
  process.env.PUBLIC_URL = PUBLIC;
  delete process.env.DASHBOARD_URL;
  mockedFetchCfg.mockResolvedValue(cfg("oidc"));
  mockedGetAuthConfig.mockResolvedValue({
    oidc_client_secret: "client-secret",
  } as Awaited<ReturnType<typeof getAuthConfig>>);
  mockedDiscovery.mockResolvedValue(DISCOVERED);
  mockedCodeGrant.mockResolvedValue({
    claims: () => ({ sub: "subject-1", email: "operator@example.com" }),
  } as unknown as Awaited<ReturnType<typeof client.authorizationCodeGrant>>);
  mockedSecret.mockResolvedValue("session-secret");
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("GET /api/auth/oidc/callback — locked", () => {
  beforeEach(() => {
    mockedFetchCfg.mockResolvedValue(cfg("locked"));
  });

  it("redirects to /login?error=auth-locked on the PUBLIC_URL host", async () => {
    const res = await GET(callbackReq());

    expect(res.status).toBe(307);
    const loc = location(res);
    // Not the container hostname from req.url.
    expect(loc.origin).toBe(PUBLIC);
    expect(loc.pathname).toBe("/login");
    expect(loc.searchParams.get("error")).toBe("auth-locked");
  });

  it("falls back to DASHBOARD_URL when PUBLIC_URL is unset", async () => {
    delete process.env.PUBLIC_URL;
    process.env.DASHBOARD_URL = "https://dash.example.test/";

    const loc = location(await GET(callbackReq()));

    expect(loc.origin).toBe("https://dash.example.test");
    expect(loc.searchParams.get("error")).toBe("auth-locked");
  });

  it("sets no session cookie", async () => {
    const res = await GET(callbackReq());

    expect(setCookie(res, SESSION_COOKIE)).toBeUndefined();
    expect(mockedSecret).not.toHaveBeenCalled();
  });

  it("clears the one-shot state cookie", async () => {
    const res = await GET(callbackReq());

    const cleared = setCookie(res, STATE_COOKIE);
    expect(cleared).toBeDefined();
    expect(cleared).toMatch(new RegExp(`^${STATE_COOKIE}=;`));
    expect(cleared).toMatch(/Max-Age=0/i);
    expect(cleared).toMatch(/Path=\//i);
  });

  it("never contacts the IdP: no discovery, no token exchange", async () => {
    await GET(callbackReq());

    expect(mockedDiscovery).not.toHaveBeenCalled();
    expect(mockedCodeGrant).not.toHaveBeenCalled();
  });

  it("decides on a forced (uncached) auth-config read", async () => {
    await GET(callbackReq());

    expect(mockedFetchCfg).toHaveBeenCalledTimes(1);
    expect(mockedFetchCfg).toHaveBeenCalledWith(true);
  });
});

describe("GET /api/auth/oidc/callback — not locked", () => {
  it("happy path: exchanges the code, sets the session cookie, clears state", async () => {
    const res = await GET(callbackReq());

    expect(res.status).toBe(307);
    const loc = location(res);
    expect(loc.origin).toBe(PUBLIC);
    expect(loc.pathname).toBe("/trades");

    expect(mockedDiscovery).toHaveBeenCalledTimes(1);
    expect(mockedCodeGrant).toHaveBeenCalledTimes(1);
    const [config, callbackUrl, checks] = mockedCodeGrant.mock.calls[0];
    expect(config).toBe(DISCOVERED);
    // Rebuilt on the public origin so redirect_uri matches the start leg.
    const cb = new URL(String(callbackUrl));
    expect(cb.origin).toBe(PUBLIC);
    expect(cb.pathname).toBe("/api/auth/oidc/callback");
    expect(cb.searchParams.get("code")).toBe("auth-code");
    expect(checks).toMatchObject({
      expectedState: "state-string",
      pkceCodeVerifier: "verifier",
    });

    const session = setCookie(res, SESSION_COOKIE);
    expect(session).toBeDefined();
    expect(session).not.toMatch(new RegExp(`^${SESSION_COOKIE}=;`));
    expect(setCookie(res, STATE_COOKIE)).toMatch(/Max-Age=0/i);
  });

  it.each<[string, Request]>([
    ["oidc-state-missing", callbackReq(null)],
    ["oidc-state-invalid", callbackReq("not-a-bundle")],
  ])("%s: refuses before any config read or IdP contact", async (code, req) => {
    const res = await GET(req);

    const loc = location(res);
    expect(loc.origin).toBe(PUBLIC);
    expect(loc.searchParams.get("error")).toBe(code);
    expect(res.headers.getSetCookie()).toEqual([]);
    expect(mockedFetchCfg).not.toHaveBeenCalled();
    expect(mockedDiscovery).not.toHaveBeenCalled();
  });

  it("oidc-misconfigured: no session cookie, state cookie left alone", async () => {
    mockedFetchCfg.mockResolvedValue({ ...cfg("oidc"), oidc_issuer: "" });

    const res = await GET(callbackReq());

    expect(location(res).searchParams.get("error")).toBe("oidc-misconfigured");
    expect(res.headers.getSetCookie()).toEqual([]);
    expect(mockedDiscovery).not.toHaveBeenCalled();
    expect(mockedCodeGrant).not.toHaveBeenCalled();
  });

  it("oidc-token-exchange-failed: no session cookie", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    mockedCodeGrant.mockRejectedValueOnce(new Error("invalid_grant"));

    const res = await GET(callbackReq());

    expect(location(res).searchParams.get("error")).toBe(
      "oidc-token-exchange-failed",
    );
    expect(setCookie(res, SESSION_COOKIE)).toBeUndefined();
  });

  it("oidc-no-claims: no session cookie", async () => {
    mockedCodeGrant.mockResolvedValueOnce({
      claims: () => undefined,
    } as unknown as Awaited<ReturnType<typeof client.authorizationCodeGrant>>);

    const res = await GET(callbackReq());

    expect(location(res).searchParams.get("error")).toBe("oidc-no-claims");
    expect(setCookie(res, SESSION_COOKIE)).toBeUndefined();
  });

  it("oidc-session-secret-unavailable: no session cookie", async () => {
    mockedSecret.mockResolvedValueOnce("");

    const res = await GET(callbackReq());

    expect(location(res).searchParams.get("error")).toBe(
      "oidc-session-secret-unavailable",
    );
    expect(setCookie(res, SESSION_COOKIE)).toBeUndefined();
  });
});
