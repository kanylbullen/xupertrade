/**
 * Tests for POST /api/tenant/me/bots/[id]/start.
 *
 * Focused on the route's pre-checks (404, 409, mode validation).
 * The decrypt+start happy path goes through the shared
 * decryptAndStart helper which is exercised against real DB +
 * docker only during deploy verification — unit-mocking it would
 * test the mock, not the code.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

// db.select() chain — terminal `.limit(1)` returns a thenable for
// the row-load. `.where()` is also reachable but we don't await it
// directly in tests using this chain (paper mode requires no
// secrets, so the secrets-check branch is skipped).
const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
// db.update() chain — terminal `.returning()` returns a thenable
// for the atomic-claim and also for the revert-claim path. We
// queue results via mockResolvedValueOnce per test.
const updateChain = {
  set: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  returning: vi.fn(),
  catch: vi.fn().mockResolvedValue(undefined),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
    update: vi.fn(() => updateChain),
  },
  tenantBots: {
    id: "id",
    tenantId: "tenantId",
    isRunning: "isRunning",
  },
  tenantSecrets: {
    key: "key",
    tenantId: "tenantId",
  },
}));
// vi.mock paths are resolved relative to the FILE BEING MOCKED's
// location, not the test file. The route at
// src/app/api/tenant/me/bots/[id]/start/route.ts imports
// "../../_decrypt-and-start". From this test's vantage point we
// reference the same module via the path that resolves to the same
// absolute file.
vi.mock("../_decrypt-and-start", () => ({
  decryptAndStart: vi.fn(),
}));

import { requireTenant } from "@/lib/tenant";

import { decryptAndStart } from "../_decrypt-and-start";
import { POST } from "../[id]/start/route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedDecryptAndStart = vi.mocked(decryptAndStart);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";
const BOT_ID = "11111111-2222-3333-4444-555566667777";

function makeReq(): Request {
  return new Request(`https://example.com/api/tenant/me/bots/${BOT_ID}/start`, {
    method: "POST",
  });
}

function makeCtx() {
  return { params: Promise.resolve({ id: BOT_ID }) };
}

function makeTenant() {
  return { id: TENANT_ID } as Awaited<ReturnType<typeof requireTenant>>;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/tenant/me/bots/[id]/start", () => {
  it("returns 404 when bot row not found", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(404);
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("returns 409 when bot is already running", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "paper", isRunning: true },
    ]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(409);
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("returns 500 when row has invalid mode", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "garbage", isRunning: false },
    ]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(500);
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("delegates to decryptAndStart on the happy path", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "paper", isRunning: false },
    ]);
    // Atomic claim wins (1 row updated).
    updateChain.returning.mockResolvedValueOnce([{ id: BOT_ID }]);
    const fakeBot = {
      id: BOT_ID,
      tenantId: TENANT_ID,
      mode: "paper",
      isRunning: true,
    };
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "ok",
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      bot: fakeBot as any,
    });

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.bot).toEqual(fakeBot);
    expect(mockedDecryptAndStart).toHaveBeenCalledWith({
      req: expect.any(Request),
      tenant: expect.objectContaining({ id: TENANT_ID }),
      botId: BOT_ID,
      mode: "paper",
    });
  });

  it("returns 409 when the atomic claim loses (concurrent /start)", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "paper", isRunning: false },
    ]);
    // Claim returns 0 rows — another POST won the race.
    updateChain.returning.mockResolvedValueOnce([]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(409);
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("forwards decryptAndStart error response to caller and reverts claim", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "paper", isRunning: false },
    ]);
    updateChain.returning.mockResolvedValueOnce([{ id: BOT_ID }]); // claim
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "response",
      response: Response.json({ error: "tenant locked" }, { status: 401 }),
    });

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(401);
    // Revert UPDATE was called — set isRunning=false / containerId=null.
    expect(updateChain.set).toHaveBeenLastCalledWith(
      expect.objectContaining({ isRunning: false, containerId: null }),
    );
  });

  it("returns 422 when required secret is missing for the row's mode", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    // Two .select() chain calls in order:
    //   1. load bot row → .from().where().limit()
    //   2. secrets-check → .from().where()  (awaited directly)
    // We override `.where` to return `selectChain` for #1 (so .limit
    // is reachable) and a Promise for #2 (the route awaits it).
    selectChain.where
      .mockReturnValueOnce(selectChain)
      .mockReturnValueOnce(Promise.resolve([])); // empty secrets list
    selectChain.limit.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "testnet",
        isRunning: false,
      },
    ]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(422);
    const body = await res.json();
    expect(body.error).toContain("HYPERLIQUID_PRIVATE_KEY");
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
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
