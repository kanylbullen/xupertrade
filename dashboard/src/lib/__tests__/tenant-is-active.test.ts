/**
 * Unit tests for security audit M-2: enforce `tenants.is_active`
 * across session resolvers.
 *
 * Before this fix `tenants.is_active` was a dead column — neither
 * `getCurrentTenant` nor `requireTenantServer` checked the flag, and
 * `requireTenant` therefore happily handed disabled tenants the same
 * tenant row as active ones. An operator who flipped `is_active=false`
 * to offboard a user got no actual revocation; the user kept logging
 * in, unlocking, and operating bots.
 *
 * These tests cover the API-route gate (`requireTenant`):
 *   (a) active tenant → returns the row as before (no regression).
 *   (b) inactive tenant → throws 403 `{error: "tenant-disabled"}`
 *       (NOT the standard 401 — distinct status so the dashboard can
 *       show a clear "your account has been disabled" message instead
 *       of bouncing back to /login forever).
 *   (c) operator-mode requests still work even if the operator's tenant
 *       row is inactive — operators are special: locking them out via
 *       a fat-fingered `is_active=false` would leave the platform with
 *       no in-band recovery. Verified through `requireOperator`.
 *
 * The auth + db layers are mocked so we exercise the resolver logic
 * in isolation, the same shape used by `tenant-server.test.ts` and
 * `operator.test.ts`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../auth", () => ({
  SESSION_COOKIE: "hypertrade_session",
  getSessionSecret: vi.fn(),
  verifySession: vi.fn(),
}));

vi.mock("../session-store", () => ({
  isSessionRevoked: vi.fn().mockResolvedValue(false),
}));

vi.mock("../db", () => ({
  db: {
    select: vi.fn(),
    insert: vi.fn(),
  },
  tenants: { authentikSub: {} },
}));

import { getSessionSecret, verifySession } from "../auth";
import { db } from "../db";
import { requireTenant } from "../tenant";
import { requireOperator } from "../operator";

const mockedGetSecret = vi.mocked(getSessionSecret);
const mockedVerify = vi.mocked(verifySession);
const mockedSelect = vi.mocked(db.select);
const mockedInsert = vi.mocked(db.insert);

afterEach(() => {
  vi.clearAllMocks();
});

function makeReq(): Request {
  return new Request("https://example.com/api/tenant/me", {
    headers: { cookie: "hypertrade_session=signed.cookie.value" },
  });
}

function chainSelect(rows: unknown[]) {
  // db.select().from(tenants).where(...).limit(1)
  const limit = vi.fn().mockResolvedValue(rows);
  const where = vi.fn().mockReturnValue({ limit });
  const from = vi.fn().mockReturnValue({ where });
  mockedSelect.mockReturnValue({ from } as never);
}

function setSession(sub: string) {
  mockedGetSecret.mockResolvedValue("secret");
  mockedVerify.mockReturnValue({ sub, iat: 1, exp: 9999999999 });
}

describe("requireTenant — M-2 is_active enforcement", () => {
  it("(a) returns the row for an active tenant (no behavior change)", async () => {
    setSession("active-user@example.com");
    const row = {
      id: "11111111-1111-1111-1111-111111111111",
      authentikSub: "active-user@example.com",
      isOperator: false,
      isActive: true,
    };
    chainSelect([row]);

    const t = await requireTenant(makeReq());
    expect(t).toBe(row);
    // Did NOT auto-create — existing active row found.
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("(b) throws 403 {error: 'tenant-disabled'} when is_active=false", async () => {
    setSession("disabled-user@example.com");
    const row = {
      id: "22222222-2222-2222-2222-222222222222",
      authentikSub: "disabled-user@example.com",
      isOperator: false,
      isActive: false,
    };
    chainSelect([row]);

    let thrown: unknown = null;
    try {
      await requireTenant(makeReq());
    } catch (e) {
      thrown = e;
    }

    expect(thrown).toBeInstanceOf(Response);
    const res = thrown as Response;
    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body).toEqual({ error: "tenant-disabled" });

    // CRITICAL regression guard: must NOT silently re-create the tenant
    // via the autoCreate path. A `eq(isActive, true)` in the WHERE
    // would have made the row look "missing" and triggered insert —
    // exactly the bug the fix avoids. If this assertion ever fails,
    // someone moved the check into the SQL.
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("(b') treats falsy non-true isActive as disabled (defensive)", async () => {
    // Same strict-bool spirit as the operator.ts isOperator check:
    // `null` / `0` / undefined isActive — e.g. from a botched ORM
    // change — must NOT be silently treated as active.
    setSession("weird-user@example.com");
    const row = {
      id: "33333333-3333-3333-3333-333333333333",
      authentikSub: "weird-user@example.com",
      isOperator: false,
      isActive: null as unknown as boolean,
    };
    chainSelect([row]);

    let thrown: unknown = null;
    try {
      await requireTenant(makeReq());
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(Response);
    expect((thrown as Response).status).toBe(403);
  });
});

describe("requireOperator — M-2 operator-bypass semantics", () => {
  it("(c) operator with is_active=false can still access operator routes", async () => {
    // Operators are special: an `is_active=false` flip on the operator
    // row must NOT lock them out, otherwise a botched offboarding (or
    // a fat-fingered Options-page toggle) leaves the platform with no
    // in-band path to recovery. Verified by passing through the same
    // resolver chain a real /api/tls/config request would use.
    setSession("operator@example.com");
    const operatorRow = {
      id: "00000000-0000-0000-0000-000000000001",
      authentikSub: "operator@example.com",
      isOperator: true,
      isActive: false,
    };
    chainSelect([operatorRow]);

    const t = await requireOperator(makeReq());
    expect(t).toBe(operatorRow);
    // Did NOT defer to requireTenant (which would have 403'd on the
    // disabled flag). The operator-bypass path returns directly.
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("(c') non-operator with is_active=false gets 403 tenant-disabled, not operator-only", async () => {
    // A disabled non-operator hitting an operator route should surface
    // the M-2 disabled-tenant error, not the generic operator-only one
    // — the disabled state is the more relevant diagnostic for the
    // user's session. requireOperator's fall-through to requireTenant
    // is what makes this work; this test pins that ordering.
    setSession("disabled-non-op@example.com");
    const row = {
      id: "44444444-4444-4444-4444-444444444444",
      authentikSub: "disabled-non-op@example.com",
      isOperator: false,
      isActive: false,
    };
    chainSelect([row]);

    let thrown: unknown = null;
    try {
      await requireOperator(makeReq());
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(Response);
    const res = thrown as Response;
    expect(res.status).toBe(403);
    const body = await res.json();
    expect(body).toEqual({ error: "tenant-disabled" });
  });
});
