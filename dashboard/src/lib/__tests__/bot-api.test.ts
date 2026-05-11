/**
 * Unit tests for getBotApiUrl + the API_PORT_BY_MODE convention
 * (multi-tenancy Phase 6c PR δ).
 *
 * Verifies the bot-routing helper that PR ε will wire into every
 * tenant-aware data route.
 */

import { describe, expect, it } from "vitest";

import { API_PORT_BY_MODE } from "../bot-orchestrator";
import { getBotApiUrl } from "../bot-api";
import type { tenantBots } from "../db";

type TenantBotRow = typeof tenantBots.$inferSelect;

function fakeRow(overrides: Partial<TenantBotRow> = {}): TenantBotRow {
  return {
    id: "00000000-0000-0000-0000-000000000010",
    tenantId: "00000000-0000-0000-0000-000000000001",
    mode: "paper",
    containerId: "abc123",
    containerName: "hypertrade-bot-paper",
    isRunning: true,
    telegramWebhookSecret: null,
    createdAt: new Date(),
    lastStartedAt: new Date(),
    lastStoppedAt: null,
    ...overrides,
  } as TenantBotRow;
}

describe("API_PORT_BY_MODE", () => {
  it("matches the compose convention for operator's bots", () => {
    // These ports are hardcoded in docker-compose.yml under
    // services.bot-{paper,testnet,mainnet}.environment.API_PORT.
    // If they ever drift from this constant, operator's bot URLs
    // would break — keep them in lockstep.
    expect(API_PORT_BY_MODE).toEqual({
      paper: 8000,
      testnet: 8001,
      mainnet: 8002,
    });
  });
});

describe("getBotApiUrl", () => {
  it("builds host:port from container_name + mode (paper)", () => {
    expect(getBotApiUrl(fakeRow({ mode: "paper", containerName: "hypertrade-bot-paper" })))
      .toBe("http://hypertrade-bot-paper:8000");
  });

  it("builds host:port from container_name + mode (testnet)", () => {
    expect(getBotApiUrl(fakeRow({ mode: "testnet", containerName: "hypertrade-bot-testnet" })))
      .toBe("http://hypertrade-bot-testnet:8001");
  });

  it("builds host:port from container_name + mode (mainnet)", () => {
    expect(getBotApiUrl(fakeRow({ mode: "mainnet", containerName: "hypertrade-bot-mainnet" })))
      .toBe("http://hypertrade-bot-mainnet:8002");
  });

  it("works for orchestrator-spawned per-tenant bot names", () => {
    // Orchestrator-spawned bots use the pattern hypertrade-bot-<short16>-<mode>
    // (per bot-orchestrator.ts:containerName). Same routing applies.
    expect(
      getBotApiUrl(fakeRow({
        mode: "testnet",
        containerName: "hypertrade-bot-3a2f1e4caaaa1111-testnet",
      })),
    ).toBe("http://hypertrade-bot-3a2f1e4caaaa1111-testnet:8001");
  });

  it("returns null when container_name is null (bot provisioned but not started)", () => {
    expect(getBotApiUrl(fakeRow({ containerName: null }))).toBeNull();
  });

  it("returns null when container_name is empty string", () => {
    expect(getBotApiUrl(fakeRow({ containerName: "" }))).toBeNull();
  });

  it("returns null when mode is an unknown value (defensive — DB column is varchar(16))", () => {
    // Drizzle types mode as string; the DB doesn't enforce the
    // BotMode union. A future bug or rogue insert could store e.g.
    // "spot". Routing must fail closed rather than silently use an
    // arbitrary port.
    expect(getBotApiUrl(fakeRow({ mode: "spot" as never }))).toBeNull();
    expect(getBotApiUrl(fakeRow({ mode: "" as never }))).toBeNull();
  });
});
