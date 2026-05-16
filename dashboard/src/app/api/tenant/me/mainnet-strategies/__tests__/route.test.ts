/**
 * Tests for /api/tenant/me/mainnet-strategies — both the GET (returns
 * operator cap + tenant set + catalogue) and the per-strategy POST
 * (writes to Redis, rejects 409 when not in operator cap).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

const redisSadd = vi.fn();
const redisSrem = vi.fn();
const redisSmembers = vi.fn();
vi.mock("@/lib/redis", () => ({
  getRedisClient: vi.fn(() => ({
    sadd: redisSadd,
    srem: redisSrem,
    smembers: redisSmembers,
  })),
}));

import { requireTenant } from "@/lib/tenant";

import { GET } from "../route";
import { POST } from "../[name]/route";

const mockedRequireTenant = vi.mocked(requireTenant);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";
const KEY = `hypertrade:mainnet:control:enabled_strategies:${TENANT_ID}`;

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

function makeGetReq() {
  return new Request("https://example.com/api/tenant/me/mainnet-strategies");
}

function makePostReq(body: unknown) {
  return new Request(
    "https://example.com/api/tenant/me/mainnet-strategies/bb_short",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

beforeEach(() => {
  mockedRequireTenant.mockResolvedValue(makeTenant());
  redisSadd.mockReset().mockResolvedValue(1);
  redisSrem.mockReset().mockResolvedValue(1);
  redisSmembers.mockReset().mockResolvedValue([]);
  delete process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES;
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/tenant/me/mainnet-strategies", () => {
  it("returns empty operator_cap when env unset (fail-closed default)", async () => {
    const res = await GET(makeGetReq());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.operator_cap).toEqual([]);
    expect(body.tenant_enabled).toEqual([]);
    expect(Array.isArray(body.all_strategies)).toBe(true);
    expect(body.all_strategies.length).toBeGreaterThan(10);
    // Each entry has the metadata the UI needs to render a row.
    for (const s of body.all_strategies) {
      expect(typeof s.name).toBe("string");
      expect(typeof s.summary).toBe("string");
    }
  });

  it("returns parsed operator_cap from env", async () => {
    process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES = "bb_short, moon_phases";
    const res = await GET(makeGetReq());
    const body = await res.json();
    expect(body.operator_cap).toEqual(["bb_short", "moon_phases"]);
  });

  it("filters unknown env names out of operator_cap (typo-tolerant)", async () => {
    process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES = "bb_short,xtypo";
    const res = await GET(makeGetReq());
    const body = await res.json();
    expect(body.operator_cap).toEqual(["bb_short"]);
  });

  it("returns tenant_enabled from Redis, filtering unknown names", async () => {
    redisSmembers.mockResolvedValue(["bb_short", "ghost_strategy"]);
    const res = await GET(makeGetReq());
    const body = await res.json();
    expect(body.tenant_enabled).toEqual(["bb_short"]);
    expect(redisSmembers).toHaveBeenCalledWith(KEY);
  });
});

describe("POST /api/tenant/me/mainnet-strategies/[name]", () => {
  async function call(body: unknown) {
    return POST(makePostReq(body), {
      params: Promise.resolve({ name: "bb_short" }),
    });
  }

  it("enables when strategy is in operator cap", async () => {
    process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES = "bb_short,moon_phases";
    const res = await call({ enabled: true });
    expect(res.status).toBe(200);
    expect(redisSadd).toHaveBeenCalledWith(KEY, "bb_short");
  });

  it("rejects enable with 409 when strategy is NOT in operator cap", async () => {
    process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES = "moon_phases";
    const res = await call({ enabled: true });
    expect(res.status).toBe(409);
    const body = await res.json();
    expect(body.error).toBe("not_in_operator_cap");
    expect(redisSadd).not.toHaveBeenCalled();
  });

  it("rejects enable with 409 when operator cap is empty", async () => {
    // env unset = empty cap = fail-closed
    const res = await call({ enabled: true });
    expect(res.status).toBe(409);
  });

  it("disable always succeeds regardless of cap (cleanup path)", async () => {
    // No env, so cap is empty — disable must still be permitted so a
    // tenant can clear stale entries after the operator narrows the cap.
    const res = await call({ enabled: false });
    expect(res.status).toBe(200);
    expect(redisSrem).toHaveBeenCalledWith(KEY, "bb_short");
  });

  it("returns 404 for unknown strategy name", async () => {
    const res = await POST(makePostReq({ enabled: true }), {
      params: Promise.resolve({ name: "definitely_not_a_strategy" }),
    });
    expect(res.status).toBe(404);
  });

  it("returns 400 on malformed body", async () => {
    const res = await call({ wrong: 1 });
    expect(res.status).toBe(400);
  });
});
