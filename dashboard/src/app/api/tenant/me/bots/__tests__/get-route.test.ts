/**
 * Tests for GET /api/tenant/me/bots/[id] — the live-status reconcile.
 *
 * Review of #171, item 5: a start in flight holds its row with
 * container_id = "claiming" and no container exists yet. GET asked
 * Docker about "claiming", got a 404, and flipped the row to
 * not-running mid-start — releasing the max_active_bots slot to a
 * concurrent start. A fresh claim is now reported as-is; a stale one
 * (its request died) is still reconciled so it can't stick.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

vi.mock("@/lib/bot-orchestrator", () => ({
  statusBot: vi.fn(),
  stopBot: vi.fn(),
}));

vi.mock("@/lib/bot-api-key", () => ({
  clearBotApiKey: vi.fn(),
}));

const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
const updateChain = {
  set: vi.fn().mockReturnThis(),
  where: vi.fn(async () => undefined),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
    update: vi.fn(() => updateChain),
  },
  tenantBots: { id: "id", tenantId: "tenantId" },
}));

import { statusBot } from "@/lib/bot-orchestrator";
import { CLAIM_STALE_AFTER_SECONDS } from "@/lib/bot-claim";
import { db } from "@/lib/db";
import { requireTenant } from "@/lib/tenant";

import { GET } from "../[id]/route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedStatusBot = vi.mocked(statusBot);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";
const BOT_ID = "11111111-2222-3333-4444-555566667777";

function call() {
  return GET(new Request(`https://example.com/api/tenant/me/bots/${BOT_ID}`), {
    params: Promise.resolve({ id: BOT_ID }),
  });
}

function row(containerId: string | null, startedSecondsAgo: number | null) {
  return {
    id: BOT_ID,
    tenantId: TENANT_ID,
    mode: "paper",
    isRunning: true,
    containerId,
    lastStartedAt:
      startedSecondsAgo === null
        ? null
        : new Date(Date.now() - startedSecondsAgo * 1000),
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/tenant/me/bots/[id]", () => {
  it("does not reconcile a fresh claim against Docker", async () => {
    mockedRequireTenant.mockResolvedValueOnce({ id: TENANT_ID } as never);
    selectChain.limit.mockResolvedValueOnce([row("claiming", 5)]);

    const res = await call();

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.starting).toBe(true);
    expect(body.bot.isRunning).toBe(true);
    expect(mockedStatusBot).not.toHaveBeenCalled();
    expect(db.update).not.toHaveBeenCalled();
  });

  it("does not reconcile a claim with no timestamp (it can't be aged)", async () => {
    // Same rule as the reaper in reserveBotStart: a claim that can't be
    // aged is left alone; Stop clears it.
    mockedRequireTenant.mockResolvedValueOnce({ id: TENANT_ID } as never);
    selectChain.limit.mockResolvedValueOnce([row("claiming", null)]);

    const res = await call();

    expect((await res.json()).starting).toBe(true);
    expect(mockedStatusBot).not.toHaveBeenCalled();
    expect(db.update).not.toHaveBeenCalled();
  });

  it("reconciles a STALE claim like any other row, so it can't stick", async () => {
    mockedRequireTenant.mockResolvedValueOnce({ id: TENANT_ID } as never);
    selectChain.limit.mockResolvedValueOnce([
      row("claiming", CLAIM_STALE_AFTER_SECONDS + 60),
    ]);
    mockedStatusBot.mockResolvedValueOnce(null); // docker: no such container

    await call();

    expect(mockedStatusBot).toHaveBeenCalledWith("claiming");
    expect(updateChain.set).toHaveBeenCalledWith(
      expect.objectContaining({ isRunning: false, containerId: null }),
    );
  });

  it("still reconciles a real container that has disappeared", async () => {
    mockedRequireTenant.mockResolvedValueOnce({ id: TENANT_ID } as never);
    selectChain.limit.mockResolvedValueOnce([row("deadbeef", 30)]);
    mockedStatusBot.mockResolvedValueOnce(null);

    await call();

    expect(mockedStatusBot).toHaveBeenCalledWith("deadbeef");
    expect(updateChain.set).toHaveBeenCalledWith(
      expect.objectContaining({ isRunning: false }),
    );
  });
});
