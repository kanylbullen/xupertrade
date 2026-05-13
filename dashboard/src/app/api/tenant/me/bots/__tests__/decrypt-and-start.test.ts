/**
 * Tests for the shared decryptAndStart helper.
 *
 * Focused on the step-4 persist: after `startBot` returns, the
 * tenant_bots row MUST get both `containerId` AND `containerName`
 * written back. Live incident 2026-05-13: only containerId was
 * persisted, so dashboard→bot routing (`getBotApiUrl`, which keys
 * off container_name) 404'd and the UI showed "Offline" for healthy
 * bots until container_name was backfilled by hand.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireUnlockedKey: vi.fn(),
}));
vi.mock("@/lib/crypto/secrets", () => ({
  decryptSecret: vi.fn(),
}));
vi.mock("@/lib/tenant-pg-role", () => ({
  generateRolePassword: vi.fn(() => "test-password"),
  provisionRole: vi.fn().mockResolvedValue(undefined),
  tenantDatabaseUrl: vi.fn(() => "postgres://tenant@db/tenant"),
}));
vi.mock("@/lib/bot-orchestrator", async () => {
  const actual = await vi.importActual("@/lib/bot-orchestrator");
  return {
    ...actual,
    startBot: vi.fn(),
    stopBot: vi.fn(),
    getOrchestratorSystemEnv: vi.fn(() => ({})),
  };
});

const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockResolvedValue([]),  // no secrets to decrypt
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
  tenantBots: { id: "id", tenantId: "tenantId" },
  tenantSecrets: { tenantId: "tenantId" },
}));

import { startBot } from "@/lib/bot-orchestrator";
import { requireUnlockedKey } from "@/lib/tenant";

import { decryptAndStart } from "../_decrypt-and-start";

const mockedStartBot = vi.mocked(startBot);
const mockedRequireUnlockedKey = vi.mocked(requireUnlockedKey);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";
const BOT_ID = "11111111-2222-3333-4444-555566667777";
const CONTAINER_ID = "deadbeefcafebabe1234";
const CONTAINER_NAME = "hypertrade-bot-3a2f1e4caaaabbbb-paper";

afterEach(() => {
  vi.clearAllMocks();
  // Reset the secrets-fetch chain to its default (no secrets) — some
  // tests may have replaced .where with a custom resolved value.
  selectChain.where.mockResolvedValue([]);
});

function makeArgs() {
  return {
    req: new Request("https://example.com/x", { method: "POST" }),
    tenant: {
      id: TENANT_ID,
      authentikSub: "sub",
      multiBotEnabled: true,
    } as Parameters<typeof decryptAndStart>[0]["tenant"],
    botId: BOT_ID,
    mode: "paper" as const,
  };
}

describe("decryptAndStart", () => {
  it("persists containerId AND containerName on successful start", async () => {
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32));
    mockedStartBot.mockResolvedValueOnce({
      id: CONTAINER_ID,
      name: CONTAINER_NAME,
      image: "hypertrade-bot:latest",
      state: "running",
      status: "Up 1 second",
      labels: {},
    });
    updateChain.returning.mockResolvedValueOnce([
      {
        id: BOT_ID,
        tenantId: TENANT_ID,
        mode: "paper",
        isRunning: true,
        containerId: CONTAINER_ID,
        containerName: CONTAINER_NAME,
      },
    ]);

    const result = await decryptAndStart(makeArgs());

    expect(result.kind).toBe("ok");
    // The CRITICAL assertion: BOTH columns are written, with the
    // values from docker's ContainerInfo (not re-derived locally).
    expect(updateChain.set).toHaveBeenCalledWith(
      expect.objectContaining({
        containerId: CONTAINER_ID,
        containerName: CONTAINER_NAME,
        isRunning: true,
      }),
    );
  });

  it("compensates by stopping the container if the persist UPDATE returns 0 rows", async () => {
    mockedRequireUnlockedKey.mockResolvedValueOnce(Buffer.alloc(32));
    mockedStartBot.mockResolvedValueOnce({
      id: CONTAINER_ID,
      name: CONTAINER_NAME,
      image: "hypertrade-bot:latest",
      state: "running",
      status: "Up 1 second",
      labels: {},
    });
    // Row vanished mid-start → returning() yields []. Helper must
    // compensate by stopping the now-orphaned container.
    updateChain.returning.mockResolvedValueOnce([]);
    const { stopBot } = await import("@/lib/bot-orchestrator");
    const mockedStopBot = vi.mocked(stopBot);

    const result = await decryptAndStart(makeArgs());

    expect(result.kind).toBe("response");
    if (result.kind === "response") expect(result.response.status).toBe(409);
    expect(mockedStopBot).toHaveBeenCalledWith(CONTAINER_ID);
  });
});
