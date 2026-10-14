/**
 * The OIDC invite gate end to end (roadmap NU-8 point 2): an IdP user
 * without `OIDC_REQUIRED_GROUP` gets no tenant.
 *
 * The callback does not decide anything — it copies the ID token's
 * `groups` claim into the signed session cookie. The tenant resolvers
 * decide on first sight. So this drives the real callback, takes the
 * real cookie it sets, and feeds that to both resolvers: the API one
 * (`getCurrentTenant` / `requireTenant`) and the page one
 * (`requireTenantServer`). Only the IdP, Redis-backed config reads,
 * the session-revocation store and the DB are mocked; signing,
 * verification and the gate run for real.
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

vi.mock("@/lib/auth-config", () => ({
  getAuthConfig: vi.fn(),
}));

vi.mock("@/lib/session-store", () => ({
  isSessionRevoked: vi.fn().mockResolvedValue(false),
}));

vi.mock("@/lib/db", () => ({
  db: { select: vi.fn(), insert: vi.fn() },
  tenants: { authentikSub: {} },
}));

vi.mock("next/headers", () => ({
  cookies: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  redirect: vi.fn((path: string) => {
    const err = new Error(`NEXT_REDIRECT;${path}`);
    err.name = "NEXT_REDIRECT";
    throw err;
  }),
  notFound: vi.fn(),
}));

import { cookies } from "next/headers";
import * as client from "openid-client";

import {
  SESSION_COOKIE,
  fetchAuthConfig,
  getSessionSecret,
  verifySession,
  type AuthConfig,
} from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";
import { db } from "@/lib/db";
import { STATE_COOKIE, encodeStateBundle } from "@/lib/oidc";
import { OIDC_GROUP_DENIED, getCurrentTenant, requireTenant } from "@/lib/tenant";
import { requireTenantServer } from "@/lib/tenant-server";

import { GET } from "../route";

const mockedCodeGrant = vi.mocked(client.authorizationCodeGrant);
const mockedSelect = vi.mocked(db.select);
const mockedInsert = vi.mocked(db.insert);

const SECRET = "session-secret";
const REQUIRED = "hypertrade-users";

const OIDC_CFG: AuthConfig = {
  mode: "oidc",
  basic_user_set: false,
  oidc_issuer: "https://idp.example.test",
  oidc_client_id: "client-id",
  oidc_scopes: "openid profile email",
};

/** Run the real callback for an IdP user carrying `groups` in the ID
 *  token, and return the session cookie value it set. */
async function signInWithGroups(groups: unknown): Promise<string> {
  mockedCodeGrant.mockResolvedValueOnce({
    claims: () => ({
      sub: "subject-1",
      email: "stranger@example.com",
      ...(groups === undefined ? {} : { groups }),
    }),
  } as unknown as Awaited<ReturnType<typeof client.authorizationCodeGrant>>);
  const state = encodeStateBundle({
    code_verifier: "verifier",
    state: "state-string",
    next: "/",
  });
  const res = await GET(
    new Request(
      "http://container-1234:3000/api/auth/oidc/callback?code=auth-code&state=state-string",
      { headers: { cookie: `${STATE_COOKIE}=${state}` } },
    ),
  );
  const set = res.headers
    .getSetCookie()
    .find((c) => c.startsWith(`${SESSION_COOKIE}=`));
  if (!set) throw new Error("callback set no session cookie");
  return set.slice(SESSION_COOKIE.length + 1).split(";")[0];
}

function apiRequest(cookie: string): Request {
  return new Request("https://dashboard.example.test/api/tenant/me", {
    headers: { cookie: `${SESSION_COOKIE}=${cookie}` },
  });
}

/** First sight: no row for the sub; after an insert, `row`. */
function firstSight(row: Record<string, unknown>) {
  let calls = 0;
  mockedSelect.mockImplementation(() => {
    calls += 1;
    const limit = vi.fn().mockResolvedValue(calls === 1 ? [] : [row]);
    return { from: () => ({ where: () => ({ limit }) }) } as never;
  });
  const values = vi.fn().mockReturnValue({
    onConflictDoNothing: vi.fn().mockResolvedValue(undefined),
  });
  mockedInsert.mockReturnValue({ values } as never);
  return { values };
}

beforeEach(() => {
  vi.stubEnv("PUBLIC_URL", "https://public.example.test");
  vi.stubEnv("OIDC_REQUIRED_GROUP", REQUIRED);
  vi.mocked(fetchAuthConfig).mockResolvedValue(OIDC_CFG);
  vi.mocked(getAuthConfig).mockResolvedValue({
    oidc_client_secret: "client-secret",
  } as Awaited<ReturnType<typeof getAuthConfig>>);
  vi.mocked(client.discovery).mockResolvedValue(
    {} as unknown as client.Configuration,
  );
  vi.mocked(getSessionSecret).mockResolvedValue(SECRET);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

describe("OIDC invite gate — a user without the group gets no tenant", () => {
  it.each<[string, unknown]>([
    ["other groups only", ["everyone", "admins"]],
    ["the group in different case", ["Hypertrade-Users"]],
    ["no groups claim at all", undefined],
    ["an empty groups claim", []],
  ])("%s: API resolver denies and inserts nothing", async (_label, groups) => {
    const cookie = await signInWithGroups(groups);
    firstSight({});

    expect(await getCurrentTenant(apiRequest(cookie))).toBe(OIDC_GROUP_DENIED);
    expect(mockedInsert).not.toHaveBeenCalled();

    firstSight({}); // fresh select sequence: still no row for the sub
    const denied = await requireTenant(apiRequest(cookie)).catch((e) => e);
    expect(denied).toBeInstanceOf(Response);
    expect((denied as Response).status).toBe(403);
    expect(await (denied as Response).json()).toEqual({
      error: "oidc-not-in-required-group",
      required_group: REQUIRED,
    });
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("page resolver redirects to /login with the reason and inserts nothing", async () => {
    const cookie = await signInWithGroups(["everyone"]);
    vi.mocked(cookies).mockResolvedValue({
      get: () => ({ value: cookie }),
    } as never);
    firstSight({});

    await expect(requireTenantServer()).rejects.toThrow(
      /NEXT_REDIRECT;\/login\?error=oidc-not-in-required-group/,
    );
    expect(mockedInsert).not.toHaveBeenCalled();
  });
});

describe("OIDC invite gate — a user in the group gets a tenant with no bots", () => {
  it.each<[string, unknown]>([
    ["groups as an array", ["everyone", REQUIRED]],
    ["groups as a single string (some IdPs)", REQUIRED],
  ])("%s", async (_label, groups) => {
    const cookie = await signInWithGroups(groups);
    // The callback normalises the claim to an array in the cookie.
    expect(verifySession(cookie, SECRET)?.groups).toContain(REQUIRED);

    const row = { id: "t-1", authentikSub: "stranger@example.com", isActive: true };
    const { values } = firstSight(row);

    expect(await getCurrentTenant(apiRequest(cookie))).toBe(row);
    expect(values).toHaveBeenCalledWith(
      expect.objectContaining({
        authentikSub: "stranger@example.com",
        maxActiveBots: 0,
      }),
    );
  });
});
