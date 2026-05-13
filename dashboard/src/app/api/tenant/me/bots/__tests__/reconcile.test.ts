/**
 * Tests for POST /api/tenant/me/bots/[id]/reconcile.
 *
 * Focused on pre-checks (404 / 409) + happy-path proxy. The actual
 * reconcile logic is exercised in bot/tests/test_reconcile/.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
  },
  tenantBots: {
    id: "id",
    tenantId: "tenantId",
  },
}));

vi.mock("@/lib/bot-api", () => ({
  getBotApiUrl: vi.fn(),
}));

import { getBotApiUrl } from "@/lib/bot-api";
import { requireTenant } from "@/lib/tenant";

import { POST } from "../[id]/reconcile/route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedGetBotApiUrl = vi.mocked(getBotApiUrl);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";
const BOT_ID = "11111111-2222-3333-4444-555566667777";

function makeReq(body?: unknown): Request {
  return new Request(
    `https://example.com/api/tenant/me/bots/${BOT_ID}/reconcile`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
  );
}

function makeCtx() {
  return { params: Promise.resolve({ id: BOT_ID }) };
}

function makeTenant() {
  return { id: TENANT_ID } as Awaited<ReturnType<typeof requireTenant>>;
}

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("POST /api/tenant/me/bots/[id]/reconcile", () => {
  it("returns 404 when bot row not found", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(404);
  });

  it("returns 409 when bot is not running", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "testnet", isRunning: false },
    ]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(409);
  });

  it("returns 404 when container has no resolvable URL", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "testnet", isRunning: true },
    ]);
    mockedGetBotApiUrl.mockReturnValueOnce(null);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(404);
  });

  it("proxies to bot and forwards JSON response", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "testnet",
        isRunning: true,
        containerName: "hypertrade-bot-x-testnet",
      },
    ]);
    mockedGetBotApiUrl.mockReturnValueOnce("http://hypertrade-bot-x-testnet:8001");

    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ examined: 5, inserted: 2, skipped: 3, inserted_ids: [10, 11] }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const res = await POST(makeReq({ since_ms: 1700000000000 }), makeCtx());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body).toEqual({
      examined: 5,
      inserted: 2,
      skipped: 3,
      inserted_ids: [10, 11],
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://hypertrade-bot-x-testnet:8001/api/control/reconcile",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ since_ms: 1700000000000 }),
      }),
    );
  });

  it("returns 502 when fetch to bot throws", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "testnet",
        isRunning: true,
        containerName: "x",
      },
    ]);
    mockedGetBotApiUrl.mockReturnValueOnce("http://x:8001");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("ECONNREFUSED")),
    );

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(502);
  });

  it("propagates 401 from requireTenant", async () => {
    mockedRequireTenant.mockRejectedValueOnce(
      new Response(JSON.stringify({ error: "not authenticated" }), {
        status: 401,
      }),
    );

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(401);
  });
});
