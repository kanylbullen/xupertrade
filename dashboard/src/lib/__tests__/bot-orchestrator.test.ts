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
  getOrchestratorSystemEnv,
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

  it("injects API_PORT per mode so per-tenant bots match the routing convention (Phase 6c PR δ)", () => {
    // Operator's compose-defined bots use 8000/8001/8002 per mode.
    // Per-tenant bots must follow the same convention so a single
    // getBotApiUrl helper works for both. The mode-pinned API_PORT is
    // assigned EXPLICITLY in buildSpec after the systemEnv/secrets
    // spread (step 4 in the override order documented inline) so
    // neither user-supplied secrets nor systemEnv can clobber it.
    for (const [mode, expectedPort] of [
      ["paper", 8000],
      ["testnet", 8001],
      ["mainnet", 8002],
    ] as const) {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode,
        // User tries to override via secrets — must NOT win.
        decryptedSecrets: { API_PORT: "9999" },
        // systemEnv tries to override too — must also NOT win.
        systemEnv: { API_PORT: "7777" },
      });
      expect(spec.env).toContain(`API_PORT=${expectedPort}`);
      // Also verify only one API_PORT entry — POSIX duplicates are
      // unsafe (getenv() impl-defined).
      const apiPortEntries = spec.env.filter((e) => e.startsWith("API_PORT="));
      expect(apiPortEntries).toHaveLength(1);
    }
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

describe("getOrchestratorSystemEnv", () => {
  const ORIG = { ...process.env };
  afterEach(() => {
    process.env = { ...ORIG };
  });

  it("returns compose-default values when no overrides set", () => {
    delete process.env.HYPERTRADE_BOT_REDIS_URL;
    delete process.env.HYPERTRADE_BOT_PAPER_INITIAL_BALANCE;
    delete process.env.HYPERTRADE_BOT_POLL_INTERVAL_SECONDS;
    delete process.env.HYPERTRADE_BOT_MAX_POSITION_SIZE_USD;
    delete process.env.HYPERTRADE_BOT_MAX_DAILY_LOSS_USD;
    delete process.env.HYPERTRADE_BOT_KILL_SWITCH;
    delete process.env.DASHBOARD_URL;
    delete process.env.API_KEY;

    const env = getOrchestratorSystemEnv();
    expect(env.REDIS_URL).toBe("redis://redis:6379/0");
    expect(env.PAPER_INITIAL_BALANCE).toBe("10000");
    expect(env.POLL_INTERVAL_SECONDS).toBe("60");
    expect(env.MAX_POSITION_SIZE_USD).toBe("200");
    expect(env.MAX_DAILY_LOSS_USD).toBe("100");
    expect(env.KILL_SWITCH).toBe("false");
    expect(env.DASHBOARD_URL).toBe("http://localhost:3000");
    expect(env.API_KEY).toBe("");
  });

  it("respects HYPERTRADE_BOT_* env overrides", () => {
    process.env.HYPERTRADE_BOT_REDIS_URL = "redis://other:9999/3";
    process.env.HYPERTRADE_BOT_PAPER_INITIAL_BALANCE = "50000";
    const env = getOrchestratorSystemEnv();
    expect(env.REDIS_URL).toBe("redis://other:9999/3");
    expect(env.PAPER_INITIAL_BALANCE).toBe("50000");
    // Unspecified fields keep their defaults.
    expect(env.POLL_INTERVAL_SECONDS).toBe("60");
  });

  it("forwards dashboard's API_KEY + DASHBOARD_URL to tenant bots", () => {
    const sysKey = "test-key-abc";
    process.env.API_KEY = sysKey;
    process.env.DASHBOARD_URL = "https://example.com";
    const env = getOrchestratorSystemEnv();
    expect(env.API_KEY).toBe(sysKey);
    expect(env.DASHBOARD_URL).toBe("https://example.com");
  });

  it("buildSpec puts systemEnv AFTER decryptedSecrets — tenant can't override API_KEY", () => {
    const sysKey = "real-sys-key";
    const spoofKey = "spoof-key";
    process.env.API_KEY = sysKey;
    const spec = buildSpec({
      botId: BOT_ID,
      tenantId: TENANT_ID,
      mode: "paper",
      decryptedSecrets: { API_KEY: spoofKey },
      systemEnv: getOrchestratorSystemEnv(),
    });
    // Find all API_KEY entries; envMap dedupes so there should be
    // exactly one — the system one wins.
    const apiKeyEntries = spec.env.filter((e) => e.startsWith("API_KEY="));
    expect(apiKeyEntries).toEqual([`API_KEY=${sysKey}`]);
  });
});
