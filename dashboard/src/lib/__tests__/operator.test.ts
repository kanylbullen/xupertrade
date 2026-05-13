/**
 * Unit tests for requireOperator (multi-tenancy Phase 6c PR γ).
 *
 * Mocks the tenant resolver so we can exercise:
 *   - operator tenant → resolved tenant returned
 *   - non-operator tenant → 403 Response thrown
 *   - no session → 401 Response thrown (passes through requireTenant)
 *
 * Catches regressions where someone might forget the isOperator check.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../tenant", () => ({
  requireTenant: vi.fn(),
  getTenantRowBypassActive: vi.fn(),
}));

import {
  getTenantRowBypassActive,
  requireTenant,
  type Tenant,
} from "../tenant";
import { requireOperator } from "../operator";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedBypassActive = vi.mocked(getTenantRowBypassActive);

function fakeTenant(overrides: Partial<Tenant> = {}): Tenant {
  return {
    id: "11111111-2222-3333-4444-555555555555",
    authentikSub: "user@example.com",
    email: "user@example.com",
    displayName: "Test User",
    isOperator: false,
    multiBotEnabled: false,
    isActive: true,
    passphraseSalt: null,
    passphraseVerifier: null,
    createdAt: new Date(),
    lastLoginAt: null,
    ...overrides,
  } as Tenant;
}

function makeReq(): Request {
  return new Request("https://example.com/api/tls/config");
}

afterEach(() => {
  mockedRequireTenant.mockReset();
  mockedBypassActive.mockReset();
});

describe("requireOperator", () => {
  it("returns the tenant when isOperator is true", async () => {
    const operator = fakeTenant({
      id: "00000000-0000-0000-0000-000000000001",
      isOperator: true,
      email: "operator@example.com",
    });
    // M-2: requireOperator looks up the row bypass-active first; only
    // when that yields no row / non-operator does it fall through to
    // requireTenant. For operator-success, bypass-active returns the row.
    mockedBypassActive.mockResolvedValue(operator);

    await expect(requireOperator(makeReq())).resolves.toBe(operator);
    // requireTenant should NOT have been consulted — operators bypass
    // the active-flag check.
    expect(mockedRequireTenant).not.toHaveBeenCalled();
  });

  it("throws a 403 Response when tenant is authenticated but not operator", async () => {
    const regular = fakeTenant({ isOperator: false });
    mockedBypassActive.mockResolvedValue(regular);
    mockedRequireTenant.mockResolvedValue(regular);

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
    expect(body).toEqual({ error: "operator only" });
  });

  it("propagates the 401 Response from requireTenant when there is no session", async () => {
    const unauth = new Response(
      JSON.stringify({ error: "not authenticated" }),
      {
        status: 401,
        headers: { "content-type": "application/json" },
      },
    );
    mockedBypassActive.mockResolvedValue(null);
    mockedRequireTenant.mockRejectedValue(unauth);

    let thrown: unknown = null;
    try {
      await requireOperator(makeReq());
    } catch (e) {
      thrown = e;
    }

    expect(thrown).toBe(unauth);
    expect((thrown as Response).status).toBe(401);
  });

  it("rejects truthy-non-true isOperator with 403 (strict bool check)", async () => {
    // Defensive contract: only the exact boolean `true` grants
    // operator access. A truthy non-bool (e.g. the string "true" from
    // a misconfigured backfill or a future ORM change that
    // accidentally serializes booleans as strings) must NOT silently
    // promote to operator. requireOperator uses `!== true` to enforce
    // this — verify by feeding it a string-typed truthy value.
    //
    // We have to cast through `unknown` because the Drizzle row type
    // declares isOperator as boolean; the runtime check is what we're
    // exercising here, not the static type.
    const weird = fakeTenant({
      isOperator: "true" as unknown as boolean,
    });
    mockedBypassActive.mockResolvedValue(weird);
    mockedRequireTenant.mockResolvedValue(weird);

    let thrown: unknown = null;
    try {
      await requireOperator(makeReq());
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(Response);
    expect((thrown as Response).status).toBe(403);
  });

  it("rejects 1-as-isOperator with 403 (strict bool check, numeric)", async () => {
    // Same contract, different non-bool truthy value. Postgres
    // boolean → number coercion is unusual but worth defending against.
    const numeric = fakeTenant({
      isOperator: 1 as unknown as boolean,
    });
    mockedBypassActive.mockResolvedValue(numeric);
    mockedRequireTenant.mockResolvedValue(numeric);

    let thrown: unknown = null;
    try {
      await requireOperator(makeReq());
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(Response);
    expect((thrown as Response).status).toBe(403);
  });
});
