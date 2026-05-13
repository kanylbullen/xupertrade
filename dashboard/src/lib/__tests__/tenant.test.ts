/**
 * Tests for getCurrentTenant / requireTenant — security audit M-3.
 *
 * Focus: the `OIDC_REQUIRED_GROUP` autocreate gate. We exercise:
 *   (a) no required-group set → autocreate works (back-compat)
 *   (b) required-group set + groups claim contains it → autocreate works
 *   (c) required-group set + groups claim missing or doesn't contain it
 *       → 403 with `oidc-not-in-required-group`
 *   (d) existing tenant logs in fine even if groups changed — group
 *       enforcement is autocreate-only, by design
 *
 * Mocks db + auth so we can drive `getSessionFromRequest` to specific
 * payloads without reaching Redis or Postgres.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
import { getCurrentTenant, requireTenant, OIDC_GROUP_DENIED } from "../tenant";

const mockedGetSecret = vi.mocked(getSessionSecret);
const mockedVerify = vi.mocked(verifySession);
const mockedSelect = vi.mocked(db.select);
const mockedInsert = vi.mocked(db.insert);

const ORIGINAL_ENV = { ...process.env };

afterEach(() => {
  vi.clearAllMocks();
  process.env = { ...ORIGINAL_ENV };
});

beforeEach(() => {
  // Default: a valid session cookie that resolves to "newuser@example.com".
  // Per-test overrides below adjust groups + DB lookup behavior.
  mockedGetSecret.mockResolvedValue("test-secret");
  mockedVerify.mockReturnValue({
    sub: "newuser@example.com",
    iat: 1,
    exp: 9999999999,
  });
});

function makeRequest(): Request {
  return new Request("https://dashboard.example.com/api/tenant/me", {
    headers: { cookie: "hypertrade_session=stub.signed.value" },
  });
}

function setSessionGroups(groups: string[] | undefined) {
  mockedVerify.mockReturnValue({
    sub: "newuser@example.com",
    iat: 1,
    exp: 9999999999,
    ...(groups !== undefined ? { groups } : {}),
  });
}

function selectFirstSightThenInsertedRow(insertedRow: unknown) {
  // First select returns empty (no row yet); second (after insert) returns
  // the freshly-inserted row.
  let calls = 0;
  mockedSelect.mockImplementation(() => {
    calls += 1;
    const limit = vi.fn().mockResolvedValue(calls === 1 ? [] : [insertedRow]);
    const where = vi.fn().mockReturnValue({ limit });
    const from = vi.fn().mockReturnValue({ where });
    return { from } as never;
  });
  const onConflictDoNothing = vi.fn().mockResolvedValue(undefined);
  const values = vi.fn().mockReturnValue({ onConflictDoNothing });
  mockedInsert.mockReturnValue({ values } as never);
  return { values };
}

function selectExistingRow(row: unknown) {
  const limit = vi.fn().mockResolvedValue([row]);
  const where = vi.fn().mockReturnValue({ limit });
  const from = vi.fn().mockReturnValue({ where });
  mockedSelect.mockReturnValue({ from } as never);
}

describe("getCurrentTenant — M-3 OIDC group autocreate gate", () => {
  it("(a) no OIDC_REQUIRED_GROUP set: autocreates on first sight (back-compat)", async () => {
    delete process.env.OIDC_REQUIRED_GROUP;
    setSessionGroups(undefined); // pre-M-3 sessions have no groups

    const newRow = {
      id: "11111111-1111-1111-1111-111111111111",
      authentikSub: "newuser@example.com",
      isActive: true,
    };
    const { values } = selectFirstSightThenInsertedRow(newRow);

    const t = await getCurrentTenant(makeRequest());
    expect(t).toBe(newRow);
    expect(values).toHaveBeenCalledWith(
      expect.objectContaining({ authentikSub: "newuser@example.com" }),
    );
  });

  it("(a') empty OIDC_REQUIRED_GROUP is treated as unset (back-compat)", async () => {
    process.env.OIDC_REQUIRED_GROUP = "   "; // whitespace only
    setSessionGroups(undefined);

    const newRow = {
      id: "11111111-1111-1111-1111-111111111111",
      authentikSub: "newuser@example.com",
      isActive: true,
    };
    selectFirstSightThenInsertedRow(newRow);

    const t = await getCurrentTenant(makeRequest());
    expect(t).toBe(newRow);
  });

  it("(b) required-group set + groups claim contains it: autocreates", async () => {
    process.env.OIDC_REQUIRED_GROUP = "hypertrade-users";
    setSessionGroups(["other-group", "hypertrade-users", "admins"]);

    const newRow = {
      id: "22222222-2222-2222-2222-222222222222",
      authentikSub: "newuser@example.com",
      isActive: true,
    };
    const { values } = selectFirstSightThenInsertedRow(newRow);

    const t = await getCurrentTenant(makeRequest());
    expect(t).toBe(newRow);
    expect(values).toHaveBeenCalled();
  });

  it("(c1) required-group set + groups claim missing entirely: returns OIDC_GROUP_DENIED", async () => {
    process.env.OIDC_REQUIRED_GROUP = "hypertrade-users";
    setSessionGroups(undefined); // pre-M-3 cookies, or IdP doesn't emit

    selectFirstSightThenInsertedRow({}); // shouldn't even reach insert

    const t = await getCurrentTenant(makeRequest());
    expect(t).toBe(OIDC_GROUP_DENIED);
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("(c2) required-group set + groups claim missing the required value: returns OIDC_GROUP_DENIED", async () => {
    process.env.OIDC_REQUIRED_GROUP = "hypertrade-users";
    setSessionGroups(["other-group", "admins"]);

    selectFirstSightThenInsertedRow({});

    const t = await getCurrentTenant(makeRequest());
    expect(t).toBe(OIDC_GROUP_DENIED);
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("(d) existing tenant logs in fine even when groups don't include required-group", async () => {
    // The user was provisioned before M-3, or before they were added to
    // the group, or they were removed. We do NOT re-check on login —
    // revocation flows through M-2's `is_active` flag instead.
    process.env.OIDC_REQUIRED_GROUP = "hypertrade-users";
    setSessionGroups([]); // group claim present but empty

    const existingRow = {
      id: "33333333-3333-3333-3333-333333333333",
      authentikSub: "newuser@example.com",
      isActive: true,
    };
    selectExistingRow(existingRow);

    const t = await getCurrentTenant(makeRequest());
    expect(t).toBe(existingRow);
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("requireTenant maps OIDC_GROUP_DENIED to a 403 with `oidc-not-in-required-group`", async () => {
    process.env.OIDC_REQUIRED_GROUP = "hypertrade-users";
    setSessionGroups(["other-group"]);
    selectFirstSightThenInsertedRow({});

    let caught: Response | null = null;
    try {
      await requireTenant(makeRequest());
    } catch (e) {
      caught = e as Response;
    }
    expect(caught).toBeInstanceOf(Response);
    expect(caught?.status).toBe(403);
    const body = await caught!.json();
    expect(body).toEqual({ error: "oidc-not-in-required-group" });
  });
});
