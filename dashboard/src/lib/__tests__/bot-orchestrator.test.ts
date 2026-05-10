/**
 * Unit tests for the bot orchestrator (multi-tenancy Phase 3a).
 *
 * Pure-function tests for `buildSpec`, `containerName`,
 * `requiredSecretsForMode`, `isValidMode`. Docker calls are mocked
 * via `vi.mock("../docker")` so we exercise the orchestrator's logic
 * without a live Docker socket.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../docker", () => ({
  createAndStart: vi.fn(),
  inspectContainer: vi.fn(),
  stopAndRemove: vi.fn(),
}));

import * as docker from "../docker";
import {
  buildSpec,
  containerName,
  isValidMode,
  requiredSecretsForMode,
  startBot,
  statusBot,
  stopBot,
} from "../bot-orchestrator";

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";
const BOT_ID = "11111111-2222-3333-4444-555566667777";

afterEach(() => {
  vi.clearAllMocks();
});

describe("isValidMode", () => {
  it.each(["paper", "testnet", "mainnet"])("accepts %s", (mode) => {
    expect(isValidMode(mode)).toBe(true);
  });

  it.each(["live", "PAPER", "", null, undefined, 1])(
    "rejects %s",
    (bad) => {
      expect(isValidMode(bad as unknown)).toBe(false);
    },
  );
});

describe("containerName", () => {
  it("uses first 16 hex chars of tenant id + mode suffix", () => {
    // 16 hex chars = 64 bits of entropy → cross-tenant collision
    // effectively impossible (PR #43 review fix; 8 chars too short).
    expect(containerName(TENANT_ID, "mainnet")).toBe(
      "hypertrade-bot-3a2f1e4caaaabbbb-mainnet",
    );
  });

  it("strips dashes from the tenant uuid", () => {
    const name = containerName("00000000-1111-2222-3333-444444444444", "paper");
    expect(name).toBe("hypertrade-bot-0000000011112222-paper");
  });

  it("name fits Docker's 63-char limit", () => {
    expect(containerName(TENANT_ID, "mainnet").length).toBeLessThan(63);
  });
});

describe("requiredSecretsForMode", () => {
  it("paper requires nothing (in-memory exchange)", () => {
    expect(requiredSecretsForMode("paper")).toEqual([]);
  });

  it("testnet + mainnet require HYPERLIQUID_PRIVATE_KEY", () => {
    expect(requiredSecretsForMode("testnet")).toContain("HYPERLIQUID_PRIVATE_KEY");
    expect(requiredSecretsForMode("mainnet")).toContain("HYPERLIQUID_PRIVATE_KEY");
  });
});

describe("buildSpec", () => {
  it("composes env vars in the order tenant/bot/mode then secrets", () => {
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "mainnet",
      decryptedSecrets: {
        HYPERLIQUID_PRIVATE_KEY: "0xdead",
        TELEGRAM_BOT_TOKEN: "12345:abc",
      },
    });
    expect(spec.env[0]).toBe(`TENANT_ID=${TENANT_ID}`);
    expect(spec.env[1]).toBe(`BOT_ID=${BOT_ID}`);
    expect(spec.env[2]).toBe("EXCHANGE_MODE=mainnet");
    expect(spec.env).toContain("HYPERLIQUID_PRIVATE_KEY=0xdead");
    expect(spec.env).toContain("TELEGRAM_BOT_TOKEN=12345:abc");
  });

  it("sets resource limits matching the design plan defaults", () => {
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "paper",
      decryptedSecrets: {},
    });
    expect(spec.memoryBytes).toBe(512 * 1024 * 1024);
    expect(spec.nanoCpus).toBe(1_000_000_000);
    expect(spec.restartPolicy).toBe("unless-stopped");
  });

  it("labels the container for inventory queries", () => {
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "testnet",
      decryptedSecrets: {},
    });
    expect(spec.labels).toEqual({
      "hypertrade.tenant_id": TENANT_ID,
      "hypertrade.bot_id": BOT_ID,
      "hypertrade.mode": "testnet",
    });
  });

  it("name matches containerName()", () => {
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "paper",
      decryptedSecrets: {},
    });
    expect(spec.name).toBe(containerName(TENANT_ID, "paper"));
  });

  it("systemEnv overrides decryptedSecrets on collision (Phase 5b)", () => {
    // Single env entry per key (no duplicates in the array — POSIX
    // allows them but getenv() behaviour is impl-defined). systemEnv
    // wins via Object spread order so a malicious user can't sneak
    // in their own DATABASE_URL via the secret CRUD API.
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "paper",
      decryptedSecrets: { DATABASE_URL: "postgresql://attacker@evil/db" },
      systemEnv: { DATABASE_URL: "postgresql://tenant_x@postgres/hypertrade" },
    });
    const databaseUrlEntries = spec.env.filter((e) =>
      e.startsWith("DATABASE_URL="),
    );
    expect(databaseUrlEntries).toHaveLength(1);
    expect(databaseUrlEntries[0]).toBe(
      "DATABASE_URL=postgresql://tenant_x@postgres/hypertrade",
    );
  });

  it("systemEnv is omitted from env when not supplied", () => {
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "paper",
      decryptedSecrets: { FOO: "bar" },
    });
    expect(spec.env).toContain("FOO=bar");
    expect(spec.env.some((e) => e.startsWith("DATABASE_URL="))).toBe(false);
  });
});

describe("startBot delegation", () => {
  it("calls createAndStart with the built spec", async () => {
    const mockedCreate = vi.mocked(docker.createAndStart);
    mockedCreate.mockResolvedValueOnce({
      id: "abc123",
      name: "hypertrade-bot-3a2f1e4c-paper",
      image: "hypertrade-bot:latest",
      state: "running",
      status: "Up 1 second",
      labels: {},
    });

    const info = await startBot({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "paper",
      decryptedSecrets: {},
    });

    expect(mockedCreate).toHaveBeenCalledOnce();
    expect(info.id).toBe("abc123");
    const passedSpec = mockedCreate.mock.calls[0][0];
    expect(passedSpec.name).toBe("hypertrade-bot-3a2f1e4caaaabbbb-paper");
  });
});

describe("stopBot delegation", () => {
  it("calls stopAndRemove with the container id", async () => {
    const mockedStop = vi.mocked(docker.stopAndRemove);
    mockedStop.mockResolvedValueOnce(undefined);
    await stopBot("abc123");
    expect(mockedStop).toHaveBeenCalledWith("abc123");
  });
});

describe("statusBot 404 handling", () => {
  it("returns null when the container is gone (404)", async () => {
    const mockedInspect = vi.mocked(docker.inspectContainer);
    mockedInspect.mockRejectedValueOnce({ statusCode: 404 });
    const result = await statusBot("ghost-id");
    expect(result).toBeNull();
  });

  it("rethrows non-404 errors as-is", async () => {
    const mockedInspect = vi.mocked(docker.inspectContainer);
    mockedInspect.mockRejectedValueOnce(new Error("docker daemon down"));
    await expect(statusBot("any-id")).rejects.toThrow(/docker daemon down/);
  });

  it("returns the container info on success", async () => {
    const mockedInspect = vi.mocked(docker.inspectContainer);
    mockedInspect.mockResolvedValueOnce({
      id: "abc",
      name: "x",
      image: "y",
      state: "running",
      status: "Up",
      labels: {},
    });
    const result = await statusBot("abc");
    expect(result?.id).toBe("abc");
  });
});
