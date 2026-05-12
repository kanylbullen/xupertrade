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

const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
  },
  tenantBots: {},
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

  it("forwards decryptAndStart error response to caller", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      { id: BOT_ID, tenantId: TENANT_ID, mode: "paper", isRunning: false },
    ]);
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "response",
      response: Response.json({ error: "tenant locked" }, { status: 401 }),
    });

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(401);
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
