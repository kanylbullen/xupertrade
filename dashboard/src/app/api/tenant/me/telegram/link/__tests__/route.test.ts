/**
 * Tests for /api/tenant/me/telegram/link (PR 3a).
 *
 * Mocks the tenant resolver, db, and redis client so we don't
 * need live infra. Verifies the 6-digit-code lifecycle:
 *   - GET returns linked status from DB row
 *   - POST mints a code, stores in Redis with NX + 10min TTL
 *   - DELETE removes the link row
 *   - 401 propagates from requireTenant
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
const deleteChain = {
  where: vi.fn().mockReturnThis(),
  returning: vi.fn(),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
    delete: vi.fn(() => deleteChain),
  },
  tenantTelegramLinks: {
    tenantId: "tenantId",
  },
}));

const redisSet = vi.fn();
vi.mock("@/lib/redis", () => ({
  getRedisClient: vi.fn(() => ({ set: redisSet })),
}));

import { requireTenant } from "@/lib/tenant";

import { DELETE, GET, POST } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";

function makeTenant() {
  return {
    id: TENANT_ID,
    email: "test@example.com",
    displayName: "Test",
    isOperator: false,
    passphraseSalt: null,
    passphraseVerifier: null,
  } as Awaited<ReturnType<typeof requireTenant>>;
}

function makeReq(): Request {
  return new Request("https://example.com/api/tenant/me/telegram/link");
}

beforeEach(() => {
  mockedRequireTenant.mockResolvedValue(makeTenant());
});

afterEach(() => {
  vi.clearAllMocks();
  selectChain.limit.mockReset();
  deleteChain.returning.mockReset();
  redisSet.mockReset();
});

describe("GET /api/tenant/me/telegram/link", () => {
  it("returns linked=false when no row exists", async () => {
    selectChain.limit.mockResolvedValueOnce([]);
    const res = await GET(makeReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ linked: false });
  });

  it("returns linked metadata when row exists", async () => {
    const linkedAt = new Date("2026-05-12T12:00:00Z");
    selectChain.limit.mockResolvedValueOnce([
      {
        tenantId: TENANT_ID,
        telegramChatId: 123456789,
        telegramUsername: "alice",
        linkedAt,
        lastUnlockAt: null,
      },
    ]);
    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.linked).toBe(true);
    expect(body.chatId).toBe(123456789);
    expect(body.username).toBe("alice");
  });

  it("propagates 401 from requireTenant", async () => {
    mockedRequireTenant.mockReset();
    mockedRequireTenant.mockRejectedValueOnce(
      new Response(JSON.stringify({ error: "not authenticated" }), {
        status: 401,
      }),
    );
    const res = await GET(makeReq());
    expect(res.status).toBe(401);
  });
});

describe("POST /api/tenant/me/telegram/link", () => {
  it("mints a 6-digit code and stores it in Redis NX with TTL", async () => {
    redisSet.mockResolvedValueOnce("OK");
    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.code).toMatch(/^\d{6}$/);
    expect(body.expiresInSeconds).toBe(600);
    expect(redisSet).toHaveBeenCalledWith(
      `tg-link:${body.code}`,
      TENANT_ID,
      "EX",
      600,
      "NX",
    );
  });

  it("retries on Redis SET NX collision", async () => {
    // 2 collisions, then success.
    redisSet
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce("OK");
    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    expect(redisSet).toHaveBeenCalledTimes(3);
  });

  it("returns 500 when 5 consecutive code collisions occur", async () => {
    // Astronomical worst case — should still surface cleanly.
    redisSet.mockResolvedValue(null);
    const res = await POST(makeReq());
    expect(res.status).toBe(500);
    expect(redisSet).toHaveBeenCalledTimes(5);
  });
});

describe("DELETE /api/tenant/me/telegram/link", () => {
  it("returns unlinked=true when a row was removed", async () => {
    deleteChain.returning.mockResolvedValueOnce([{ tenantId: TENANT_ID }]);
    const res = await DELETE(makeReq());
    const body = await res.json();
    expect(body.unlinked).toBe(true);
  });

  it("returns unlinked=false when no row existed (idempotent)", async () => {
    deleteChain.returning.mockResolvedValueOnce([]);
    const res = await DELETE(makeReq());
    const body = await res.json();
    expect(body.unlinked).toBe(false);
  });
});
