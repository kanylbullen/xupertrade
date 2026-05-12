/**
 * Tests for /api/tenant/me/telegram/link (PR 3a).
 *
 * Mocks the tenant resolver, db, and redis client so we don't
 * need live infra. Verifies the 6-digit-code lifecycle:
 *   - GET returns linked status from DB row (bigint chat_id → string)
 *   - POST mints a code, stores in Redis with NX + 10min TTL
 *   - POST reuses existing active code (spam prevention)
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
const redisGet = vi.fn();
const redisTtl = vi.fn();
vi.mock("@/lib/redis", () => ({
  getRedisClient: vi.fn(() => ({
    set: redisSet,
    get: redisGet,
    ttl: redisTtl,
  })),
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
  // Default: no active code, so POST mints a fresh one. Tests
  // that want reuse-existing override this.
  redisGet.mockResolvedValue(null);
});

afterEach(() => {
  vi.clearAllMocks();
  selectChain.limit.mockReset();
  deleteChain.returning.mockReset();
  redisSet.mockReset();
  redisGet.mockReset();
  redisTtl.mockReset();
});

describe("GET /api/tenant/me/telegram/link", () => {
  it("returns linked=false when no row exists", async () => {
    selectChain.limit.mockResolvedValueOnce([]);
    const res = await GET(makeReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({ linked: false });
  });

  it("returns linked metadata with chatId as string (bigint -> string)", async () => {
    const linkedAt = new Date("2026-05-12T12:00:00Z");
    selectChain.limit.mockResolvedValueOnce([
      {
        tenantId: TENANT_ID,
        telegramChatId: BigInt("123456789"),
        telegramUsername: "alice",
        linkedAt,
        lastUnlockAt: null,
      },
    ]);
    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.linked).toBe(true);
    // JSON has no bigint, so we serialize as string. Test that
    // the wire format is the string and that it round-trips.
    expect(body.chatId).toBe("123456789");
    expect(typeof body.chatId).toBe("string");
    expect(body.username).toBe("alice");
  });

  it("returns negative supergroup chatId correctly as string", async () => {
    selectChain.limit.mockResolvedValueOnce([
      {
        tenantId: TENANT_ID,
        telegramChatId: BigInt("-1002345678901"),
        telegramUsername: null,
        linkedAt: new Date(),
        lastUnlockAt: null,
      },
    ]);
    const res = await GET(makeReq());
    const body = await res.json();
    expect(body.chatId).toBe("-1002345678901");
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
    redisSet.mockResolvedValueOnce("OK"); // tg-link:<code> set
    redisSet.mockResolvedValueOnce("OK"); // reverse-pointer set
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
    expect(redisSet).toHaveBeenCalledWith(
      `tg-link:tenant:${TENANT_ID}`,
      body.code,
      "EX",
      600,
    );
  });

  it("returns existing active code instead of churning Redis keys", async () => {
    // Simulate: tenant already has code "123456" with 300s left.
    redisGet.mockResolvedValueOnce("123456");
    redisTtl.mockResolvedValueOnce(300);

    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.code).toBe("123456");
    expect(body.expiresInSeconds).toBe(300);
    // No new code minted — spam vector closed.
    expect(redisSet).not.toHaveBeenCalled();
  });

  it("mints fresh code if reverse pointer exists but TTL expired", async () => {
    // Race: pointer just expired, ttl returns -2 (key gone) or 0.
    redisGet.mockResolvedValueOnce("oldcode");
    redisTtl.mockResolvedValueOnce(-2);
    redisSet.mockResolvedValueOnce("OK");
    redisSet.mockResolvedValueOnce("OK");

    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.code).not.toBe("oldcode");
    expect(redisSet).toHaveBeenCalled();
  });

  it("retries on Redis SET NX collision", async () => {
    redisSet
      .mockResolvedValueOnce(null) // collision
      .mockResolvedValueOnce(null) // collision
      .mockResolvedValueOnce("OK") // win
      .mockResolvedValueOnce("OK"); // reverse pointer
    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    // 3 NX attempts + 1 reverse-pointer set = 4 calls
    expect(redisSet).toHaveBeenCalledTimes(4);
  });

  it("returns 500 when 5 consecutive code collisions occur", async () => {
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
