/**
 * Unit tests for `requireUnlockedKey` (multi-tenancy Phase 2d gate).
 *
 * Mocks the k-cache module so we can exercise both the cached-K and
 * the locked branches without a live Redis. The DB layer is untouched
 * because `requireUnlockedKey` reads K-cache only — it takes the
 * tenant row as an argument.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../crypto/k-cache", () => ({
  loadKey: vi.fn(),
}));

import { loadKey } from "../crypto/k-cache";
import { KEY_BYTES } from "../crypto/secrets";
import { requireUnlockedKey } from "../tenant";

const mockedLoadKey = vi.mocked(loadKey);

const SIGNED_COOKIE = "abc.def";  // shape doesn't matter for these tests
const FAKE_TENANT = {
  id: "11111111-2222-3333-4444-555555555555",
} as Parameters<typeof requireUnlockedKey>[1];

function makeReq(cookieValue: string | null): Request {
  return new Request("https://example.com/api/tenant/me/secrets", {
    method: "GET",
    headers: cookieValue
      ? { cookie: `hypertrade_session=${cookieValue}` }
      : {},
  });
}

afterEach(() => {
  mockedLoadKey.mockReset();
});

describe("requireUnlockedKey", () => {
  it("returns K when k-cache has it", async () => {
    const fakeK = Buffer.alloc(KEY_BYTES, 0x42);
    mockedLoadKey.mockResolvedValueOnce(fakeK);

    const k = await requireUnlockedKey(makeReq(SIGNED_COOKIE), FAKE_TENANT);
    expect(k).toBeInstanceOf(Buffer);
    expect(k.equals(fakeK)).toBe(true);
  });

  it("throws 401 'no session' when no cookie is present", async () => {
    let thrown: unknown;
    try {
      await requireUnlockedKey(makeReq(null), FAKE_TENANT);
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(Response);
    const r = thrown as Response;
    expect(r.status).toBe(401);
    const body = await r.json();
    expect(body.error).toMatch(/no session/);
    expect(mockedLoadKey).not.toHaveBeenCalled();
  });

  it("throws 401 'tenant locked' when k-cache returns null", async () => {
    mockedLoadKey.mockResolvedValueOnce(null);

    let thrown: unknown;
    try {
      await requireUnlockedKey(makeReq(SIGNED_COOKIE), FAKE_TENANT);
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(Response);
    const r = thrown as Response;
    expect(r.status).toBe(401);
    const body = await r.json();
    expect(body.error).toMatch(/tenant locked/);
  });

  it("propagates k-cache errors as-is (DB/Redis failures aren't 401)", async () => {
    mockedLoadKey.mockRejectedValueOnce(new Error("redis down"));

    await expect(
      requireUnlockedKey(makeReq(SIGNED_COOKIE), FAKE_TENANT),
    ).rejects.toThrow(/redis down/);
  });
});
