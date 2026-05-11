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
}));

vi.mock("../auth", () => ({
  SESSION_COOKIE: "hypertrade_session",
  getSessionSecret: vi.fn(),
  verifySession: vi.fn(),
}));

vi.mock("../db", () => ({
  db: {
    select: vi.fn(),
    insert: vi.fn(),
  },
  tenants: { authentikSub: {} },
}));

import { cookies } from "next/headers";
import { getSessionSecret, verifySession } from "../auth";
import { db } from "../db";
import { requireTenantServer } from "../tenant-server";

const mockedCookies = vi.mocked(cookies);
const mockedGetSecret = vi.mocked(getSessionSecret);
const mockedVerify = vi.mocked(verifySession);
const mockedSelect = vi.mocked(db.select);
const mockedInsert = vi.mocked(db.insert);

afterEach(() => {
  vi.clearAllMocks();
});

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
    setCookie(null);
    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });

  it("redirects to /login when session secret is unavailable", async () => {
    setCookie("any.signed.value");
    mockedGetSecret.mockRejectedValue(new Error("bot unreachable"));
    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });

  it("redirects to /login when session signature is invalid", async () => {
    setCookie("tampered.value");
    mockedGetSecret.mockResolvedValue("secret");
    mockedVerify.mockReturnValue(null);
    await expect(requireTenantServer()).rejects.toThrow(/NEXT_REDIRECT;\/login/);
  });

  it("returns the existing tenant row when found", async () => {
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
    };
    chainSelectReturning([row]);

    const t = await requireTenantServer();
    expect(t).toBe(row);
    // Did NOT call insert — existing row found.
    expect(mockedInsert).not.toHaveBeenCalled();
  });

  it("auto-creates a new tenant row on first sight, then returns it", async () => {
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
      }),
    );
  });
});
