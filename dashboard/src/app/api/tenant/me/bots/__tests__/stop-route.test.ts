/**
 * Tests for POST /api/tenant/me/bots/[id]/stop.
 *
 * Mocks the DB and the docker layer so we can exercise:
 *   - 404 when the bot row doesn't exist (or belongs to another
 *     tenant)
 *   - 200 + no docker call when already stopped
 *   - 200 + stop call when running
 *   - 500 when stopBot throws
 *   - reconciliation: row marked stopped even after stopBot returns
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));
vi.mock("@/lib/bot-orchestrator", async () => {
  const actual = await vi.importActual("@/lib/bot-orchestrator");
  return { ...actual, stopBot: vi.fn() };
});

// Capture the chain DB calls; route uses .select().from().where().limit()
// for the load and .update().set().where().returning() for the persist.
const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
const updateChain = {
  set: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  returning: vi.fn(),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
    update: vi.fn(() => updateChain),
  },
  tenantBots: {},
}));

import { stopBot } from "@/lib/bot-orchestrator";
import { requireTenant } from "@/lib/tenant";

import { POST } from "../[id]/stop/route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedStopBot = vi.mocked(stopBot);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";
const BOT_ID = "11111111-2222-3333-4444-555566667777";

function makeReq(): Request {
  return new Request(`https://example.com/api/tenant/me/bots/${BOT_ID}/stop`, {
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

describe("POST /api/tenant/me/bots/[id]/stop", () => {
  it("returns 404 when bot row not found", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(404);
    expect(mockedStopBot).not.toHaveBeenCalled();
  });

  it("returns 200 + skips docker when already stopped", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "paper",
        isRunning: false,
        containerId: null,
      },
    ]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(200);
    expect(mockedStopBot).not.toHaveBeenCalled();
  });

  it("calls stopBot + reconciles row when running", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "paper",
        isRunning: true,
        containerId: "deadbeef",
      },
    ]);
    mockedStopBot.mockResolvedValueOnce(undefined);
    updateChain.returning.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "paper",
        isRunning: false,
        containerId: null,
      },
    ]);

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(200);
    expect(mockedStopBot).toHaveBeenCalledWith("deadbeef");
    // Both columns must be cleared: containerName is what
    // `getBotApiUrl` keys off for routing, so a stale value would
    // make a stopped bot look reachable. (Live incident 2026-05-13
    // motivated the symmetric Start/Stop persist contract.)
    expect(updateChain.set).toHaveBeenCalledWith(
      expect.objectContaining({
        isRunning: false,
        containerId: null,
        containerName: null,
      }),
    );
  });

  it("returns 500 when stopBot throws", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant());
    selectChain.limit.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "paper",
        isRunning: true,
        containerId: "deadbeef",
      },
    ]);
    mockedStopBot.mockRejectedValueOnce(new Error("docker socket gone"));

    const res = await POST(makeReq(), makeCtx());
    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body.error).toContain("docker socket gone");
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
