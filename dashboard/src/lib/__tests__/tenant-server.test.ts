/**
 * Unit tests for requireTenantServer (Phase 6c PR ζ).
 *
 * Mocks next/headers cookies + auth + db so we can exercise:
 *   - no session cookie → redirect to /login
 *   - invalid session signature → redirect to /login
 *   - existing tenant → return row
 *   - first-sight tenant → INSERT then return new row
 *
 * `redirect()` from next/navigation throws a special Next.js error
 * that we catch by name (NEXT_REDIRECT) — that's how Next.js
 * implements server-side redirects.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({
  cookies: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  redirect: vi.fn((path: string) => {
    const err = new Error(`NEXT_REDIRECT;${path}`);
    err.name = "NEXT_REDIRECT";
    throw err;
  }),
  notFound: vi.fn(() => {
    const err = new Error("NEXT_NOT_FOUND");
    err.name = "NEXT_NOT_FOUND";
    throw err;
  }),
}));

// requireTenantServer calls isSessionRevoked(), whose `client`
// parameter defaults to getRedisClient(). Without this mock the unit
// test opens a real Redis connection: with no Redis listening it
// fails closed (returns true) and redirects to /login, and ioredis's
// retry backoff blows the 5s test timeout. Same pattern as
// tenant.test.ts / tenant-is-active.test.ts.
vi.mock("../session-store", () => ({
  isSessionRevoked: vi.fn().mockResolvedValue(false),
}));

vi.mock("../auth", () => ({
  SESSION_COOKIE: "hypertrade_session",
  getSessionSecret: vi.fn(),
  verifySession: vi.fn(),
  fetchAuthConfig: vi.fn(),
}));

vi.mock("../db", () => ({
  db: {
    select: vi.fn(),
    insert: vi.fn(),
  },
  tenants: { authentikSub: {} },
}));

import { cookies } from "next/headers";
import { fetchAuthConfig, getSessionSecret, verifySession } from "../auth";
import { db } from "../db";
import { requireOperatorServer, requireTenantServer } from "../tenant-server";

const mockedCookies = vi.mocked(cookies);
const mockedGetSecret = vi.mocked(getSessionSecret);
const mockedVerify = vi.mocked(verifySession);
const mockedFetchAuthConfig = vi.mocked(fetchAuthConfig);
const mockedSelect = vi.mocked(db.select);
const mockedInsert = vi.mocked(db.insert);

afterEach(() => {
  vi.clearAllMocks();
});

// Default: auth is enabled (basic or oidc) — disabled-mode short-
// circuit is exercised in dedicated tests below.
function authEnabled() {
  mockedFetchAuthConfig.mockResolvedValue({
    mode: "basic",
    basic_user_set: true,
    oidc_issuer: "",
    oidc_client_id: "",
    oidc_scopes: "",
  } as never);
}

function setCookie(value: string | null) {
  const get = vi.fn().mockReturnValue(value === null ? undefined : { value });
  mockedCookies.mockResolvedValue({ get } as never);
}

function chainSelectReturning(rows: unknown[]) {
  const limit = vi.fn().mockResolvedValue(rows);
  const where = vi.fn().mockReturnValue({ limit });
  const from = vi.fn().mockReturnValue({ where });
  mockedSelect.mockReturnValue({ from } as never);
  return { from, where, limit };
}

describe("requireTenantServer", () => {
  it("redirects to /login when there is no session cookie", async () => {
    authEnabled();
    setCookie(null);
    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });

  it("redirects to /login when session secret is unavailable", async () => {
    authEnabled();
    setCookie("any.signed.value");
    mockedGetSecret.mockRejectedValue(new Error("bot unreachable"));
    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });

  it("redirects to /login when session signature is invalid", async () => {
    authEnabled();
    setCookie("tampered.value");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue(null);
    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });

  it("returns the existing tenant row when found", async () => {
    authEnabled();
    setCookie("good.cookie");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue({
      sub: "alice@example.com",
      iat: 1,
      exp: 9999999999,
    });
    const row = {
      id: "11111111-2222-3333-4444-555555555555",
      authentikSub: "alice@example.com",
      isOperator: false,
      isActive: true,
    };
    chainSelectReturning([row]);

    const t = await requireTenantServer();
    expect(t).toBe(row);
    // Did NOT call insert — existing row found.
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("auto-creates a new tenant row on first sight, then returns it", async () => {
    authEnabled();
    setCookie("good.cookie");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue({
      sub: "newuser@example.com",
      iat: 1,
      exp: 9999999999,
    });

    // First select returns empty; second (after insert) returns the row.
    const newRow = {
      id: "22222222-3333-4444-5555-666666666666",
      authentikSub: "newuser@example.com",
      isOperator: false,
      isActive: true,
    };
    let selectCalls = 0;
    mockedSelect.mockImplementation(() => {
      selectCalls += 1;
      const limit = vi.fn().mockResolvedValue(selectCalls === 1 ? [] : [newRow]);
      const where = vi.fn().mockReturnValue({ limit });
      const from = vi.fn().mockReturnValue({ where });
      return { from } as never;
    });

    // Insert chain: insert().values().onConflictDoNothing()
    const onConflict = vi.fn().mockResolvedValue(undefined);
    const values = vi.fn().mockReturnValue({ onConflictDoNothing: onConflict });
    mockedInsert.mockReturnValue({ values } as never);

    const t = await requireTenantServer();
    expect(t).toBe(newRow);
    expect(mockedInsert).toHaveBeenCalledOnce();
    expect(values).toHaveBeenCalledWith(
      expect.objectContaining({
        authentikSub: "newuser@example.com",
        email: "newuser@example.com",
        // NU-8: the page path creates tenants through the same helper
        // as the API path, so it gets the same zero bot allowance.
        maxActiveBots: 0,
      }),
    );
  });

  it("redirects with oidc-not-in-required-group and creates no tenant when the group is missing", async () => {
    // The page-side resolver had its own copy of the M-3 gate and no
    // test of it; it now shares autocreateTenant with the API path.
    vi.stubEnv("OIDC_REQUIRED_GROUP", "hypertrade-users");
    try {
      authEnabled();
      setCookie("good.cookie");
      mockedGetSecret.mockResolvedValue("secret");
      mockedVerify.mockReturnValue({
        sub: "stranger@example.com",
        iat: 1,
        exp: 9999999999,
        groups: ["everyone"],
      });
      chainSelectReturning([]);

      await expect(requireTenantServer()).rejects.toThrow(
        /NEXT_REDIRECT;\/login\?error=oidc-not-in-required-group/,
      );
      expect(mockedInsert).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("redirects to /login?error=tenant-disabled when existing row has isActive=false", async () => {
    // Copilot review fix on PR #93 — pin the new behavior so a
    // future regression doesn't silently let disabled tenants in.
    authEnabled();
    setCookie("good.cookie");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue({
      sub: "disabled@example.com",
      iat: 1,
      exp: 9999999999,
    });
    chainSelectReturning([{
      id: "33333333-4444-5555-6666-777777777777",
      authentikSub: "disabled@example.com",
      isOperator: false,
      isActive: false,
    }]);

    await expect(requireTenantServer()).rejects.toThrow(
      /NEXT_REDIRECT;\/login\?error=tenant-disabled/,
    );
  });

  it("redirects to /login?error=tenant-disabled when post-insert re-select returns isActive=false", async () => {
    // Race: first select returns empty (looks brand-new), insert with
    // onConflictDoNothing succeeds, second select hits the row that
    // ANOTHER request created with isActive already flipped to false.
    // Should still 403-redirect, not return the disabled row.
    authEnabled();
    setCookie("good.cookie");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue({
      sub: "race@example.com",
      iat: 1,
      exp: 9999999999,
    });
    const reselectedRow = {
      id: "44444444-5555-6666-7777-888888888888",
      authentikSub: "race@example.com",
      isOperator: false,
      isActive: false,
    };
    let selectCalls = 0;
    mockedSelect.mockImplementation(() => {
      selectCalls += 1;
      const limit = vi.fn().mockResolvedValue(selectCalls === 1 ? [] : [reselectedRow]);
      const where = vi.fn().mockReturnValue({ limit });
      const from = vi.fn().mockReturnValue({ where });
      return { from } as never;
    });
    const onConflict = vi.fn().mockResolvedValue(undefined);
    const values = vi.fn().mockReturnValue({ onConflictDoNothing: onConflict });
    mockedInsert.mockReturnValue({ values } as never);

    await expect(requireTenantServer()).rejects.toThrow(
      /NEXT_REDIRECT;\/login\?error=tenant-disabled/,
    );
  });

  it("returns operator tenant in disabled-auth mode (no cookie required)", async () => {
    // proxy.ts lets all requests through when cfg.mode === "disabled".
    // requireTenantServer must mirror that — resolving the operator
    // tenant rather than redirecting to a login that doesn't exist.
    mockedFetchAuthConfig.mockResolvedValue({
      mode: "disabled",
      basic_user_set: false,
      oidc_issuer: "",
      oidc_client_id: "",
      oidc_scopes: "",
    } as never);
    const operator = {
      id: "00000000-0000-0000-0000-000000000001",
      authentikSub: "operator@example.com",
      isOperator: true,
    };
    chainSelectReturning([operator]);
    // No cookie set on purpose — disabled mode must not require one.
    setCookie(null);

    const t = await requireTenantServer();
    expect(t).toBe(operator);
  });

  it("redirects with auth-locked and never touches the DB in locked mode", async () => {
    // SECURITY: "locked" means the stored auth mode is gone on an
    // install that has tenants. The operator row this helper would
    // otherwise resolve IS the data being withheld, so we must bail
    // before any select.
    mockedFetchAuthConfig.mockResolvedValue({
      mode: "locked",
      basic_user_set: false,
      oidc_issuer: "",
      oidc_client_id: "",
      oidc_scopes: "",
    } as never);
    setCookie(null);

    await expect(requireTenantServer()).rejects.toThrow(
      /NEXT_REDIRECT;\/login\?error=auth-locked/,
    );
    expect(mockedSelect).not.toHaveBeenCalled();
  });

  it("falls back to cookie path if disabled-mode operator row is missing", async () => {
    // Defensive: if cfg.mode is "disabled" but Phase 6b never ran, the
    // operator row doesn't exist. Don't silently render with no
    // tenant — let the cookie path run, which will redirect to /login
    // (failing closed).
    mockedFetchAuthConfig.mockResolvedValue({
      mode: "disabled",
      basic_user_set: false,
      oidc_issuer: "",
      oidc_client_id: "",
      oidc_scopes: "",
    } as never);
    chainSelectReturning([]);
    setCookie(null);

    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });
});

/**
 * analysis-2026-09-15 § 5, Low. The /admin page shells relied on
 * `admin/layout.tsx` alone, and a layout is not a gate: in the App
 * Router it renders concurrently with the page beneath it, so its
 * notFound() stops the response but not the page body's own work.
 */
describe("requireOperatorServer", () => {
  function signedInAs(row: Record<string, unknown>) {
    authEnabled();
    setCookie("good.cookie");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue({
      sub: "someone@example.com",
      iat: 1,
      exp: 9999999999,
    });
    chainSelectReturning([row]);
  }

  it("returns the tenant when they are the operator", async () => {
    const operator = {
      id: "00000000-0000-0000-0000-000000000001",
      authentikSub: "someone@example.com",
      isOperator: true,
      isActive: true,
    };
    signedInAs(operator);
    await expect(requireOperatorServer()).resolves.toBe(operator);
  });

  it("404s a signed-in non-operator", async () => {
    signedInAs({
      id: "11111111-2222-3333-4444-555555555555",
      authentikSub: "someone@example.com",
      isOperator: false,
      isActive: true,
    });
    await expect(requireOperatorServer()).rejects.toThrow("NEXT_NOT_FOUND");
  });

  it("404s a truthy-but-not-true isOperator", async () => {
    // Mirrors `lib/operator.ts`: a non-boolean from a misconfigured
    // backfill must not grant operator access.
    signedInAs({
      id: "11111111-2222-3333-4444-555555555555",
      authentikSub: "someone@example.com",
      isOperator: 1,
      isActive: true,
    });
    await expect(requireOperatorServer()).rejects.toThrow("NEXT_NOT_FOUND");
  });

  it("redirects to /login when there is no session at all", async () => {
    authEnabled();
    setCookie(null);
    await expect(requireOperatorServer()).rejects.toThrow(
      /NEXT_REDIRECT;\/login/,
    );
  });
});
