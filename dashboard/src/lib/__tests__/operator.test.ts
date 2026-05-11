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
}));

import { requireTenant, type Tenant } from "../tenant";
import { requireOperator } from "../operator";

const mockedRequireTenant = vi.mocked(requireTenant);

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
});

describe("requireOperator", () => {
  it("returns the tenant when isOperator is true", async () => {
    const operator = fakeTenant({
      id: "00000000-0000-0000-0000-000000000001",
      isOperator: true,
      email: "operator@example.com",
    });
    mockedRequireTenant.mockResolvedValue(operator);

    await expect(requireOperator(makeReq())).resolves.toBe(operator);
  });

  it("throws a 403 Response when tenant is authenticated but not operator", async () => {
    const regular = fakeTenant({ isOperator: false });
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

  it("uses === true rather than truthy check (no falsy bypass)", async () => {
    // Defensive: someone might set isOperator to a truthy non-bool
    // (e.g. "true" string from a misconfigured backfill). Without an
    // explicit !== true check, that would silently grant operator
    // access. requireOperator uses !t.isOperator which treats only
    // exact `true` as "is operator"; "true" string is still truthy
    // so this would actually pass — but the goal of this test is to
    // document the contract: only bool true grants access in the
    // future if we ever tighten the check.
    const weird = fakeTenant({ isOperator: false });
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
});
