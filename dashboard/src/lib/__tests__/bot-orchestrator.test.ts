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
// H-1: every buildSpec/startBot call needs an apiKey now. Tests use a
// fixed value because they exercise envMap shape, not key generation.
const TEST_API_KEY = "test-api-key";

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
      apiKey: TEST_API_KEY,
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
      apiKey: TEST_API_KEY,
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
      apiKey: TEST_API_KEY,
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
      apiKey: TEST_API_KEY,
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
        apiKey: TEST_API_KEY,
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
      apiKey: TEST_API_KEY,
    });
    const databaseUrlEntries = spec.env.filter((e) =>
      e.startsWith("DATABASE_URL="),
    );
    expect(databaseUrlEntries).toHaveLength(1);
    expect(databaseUrlEntries[0]).toBe(
      "DATABASE_URL=postgresql://tenant_x@postgres/hypertrade",
    );
  });

  describe("TELEGRAM_ENABLED mode-gate (post-PR-4c dup-notification fix)", () => {
    // After PR 4c retired the compose-bot model, every orchestrator-
    // spawned bot inherited Settings.telegram_enabled=True (the bot
    // config default). Operator saw EVERY trade.executed twice — once
    // tagged "PAPER" and once tagged "TESTNET". The legacy compose
    // model hardcoded TELEGRAM_ENABLED=false on bot-paper and
    // bot-mainnet so only bot-testnet posted (and routed events for
    // all 3 modes via channel subscriptions). buildSpec now restores
    // that single-owner convention by mode-gating TELEGRAM_ENABLED.
    it("enables Telegram on mainnet (canonical owner; co-located with vault scanner per PR #113)", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
    });
      const entries = spec.env.filter((e) => e.startsWith("TELEGRAM_ENABLED="));
      expect(entries).toEqual(["TELEGRAM_ENABLED=true"]);
    });

    it.each(["paper", "testnet"] as const)(
      "disables Telegram on %s (silenced to prevent duplicate notifications)",
      (mode) => {
        const spec = buildSpec({
          tenantId: TENANT_ID,
          botId: BOT_ID,
          mode,
          decryptedSecrets: {},
          apiKey: TEST_API_KEY,
    });
        const entries = spec.env.filter((e) =>
          e.startsWith("TELEGRAM_ENABLED="),
        );
        expect(entries).toEqual(["TELEGRAM_ENABLED=false"]);
      },
    );

    it("tenant-supplied TELEGRAM_ENABLED in decryptedSecrets cannot win over the mode gate", () => {
      // Tenant tries to flip the gate on paper-bot via the secret CRUD
      // API — must NOT win, otherwise a misconfigured tenant would
      // resurrect the dup-notification bug. The mode-derived value is
      // injected AFTER both decryptedSecrets and systemEnv spreads.
      for (const mode of ["paper", "testnet"] as const) {
        const spec = buildSpec({
          tenantId: TENANT_ID,
          botId: BOT_ID,
          mode,
          decryptedSecrets: { TELEGRAM_ENABLED: "true" },
          apiKey: TEST_API_KEY,
    });
        const entries = spec.env.filter((e) =>
          e.startsWith("TELEGRAM_ENABLED="),
        );
        expect(entries).toEqual(["TELEGRAM_ENABLED=false"]);
      }
    });

    it("systemEnv-supplied TELEGRAM_ENABLED also cannot win over the mode gate", () => {
      // A future operator who tries to flip the gate by injecting
      // TELEGRAM_ENABLED into systemEnv must also be overridden — the
      // mode is the single source of truth. (Future per-mode override
      // would need a real escape hatch like
      // HYPERTRADE_BOT_TELEGRAM_ENABLED_MODE; intentionally not added
      // in this PR.)
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "paper",
        decryptedSecrets: {},
        systemEnv: { TELEGRAM_ENABLED: "true" },
        apiKey: TEST_API_KEY,
    });
      const entries = spec.env.filter((e) =>
        e.startsWith("TELEGRAM_ENABLED="),
      );
      expect(entries).toEqual(["TELEGRAM_ENABLED=false"]);
    });

    it("tenant-supplied TELEGRAM_ENABLED=false in decryptedSecrets cannot silence mainnet", () => {
      // Symmetric to the paper/testnet case: a tenant who tries to
      // silence the mainnet notifier via the secret CRUD API must NOT
      // win, otherwise the canonical Telegram owner could be muted by
      // a tenant misconfig. The mode-derived value is injected AFTER
      // the decryptedSecrets spread.
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: { TELEGRAM_ENABLED: "false" },
        apiKey: TEST_API_KEY,
    });
      const entries = spec.env.filter((e) =>
        e.startsWith("TELEGRAM_ENABLED="),
      );
      expect(entries).toEqual(["TELEGRAM_ENABLED=true"]);
    });

    it("systemEnv-supplied TELEGRAM_ENABLED=false also cannot silence mainnet", () => {
      // Symmetric to the systemEnv=true paper case. Mode is the single
      // source of truth in both directions: operator cannot enable on
      // a silenced mode AND cannot disable on the canonical owner.
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        systemEnv: { TELEGRAM_ENABLED: "false" },
        apiKey: TEST_API_KEY,
    });
      const entries = spec.env.filter((e) =>
        e.startsWith("TELEGRAM_ENABLED="),
      );
      expect(entries).toEqual(["TELEGRAM_ENABLED=true"]);
    });
  });

  it("systemEnv is omitted from env when not supplied", () => {
    const spec = buildSpec({
      tenantId: TENANT_ID,
      botId: BOT_ID,
      mode: "paper",
      decryptedSecrets: { FOO: "bar" },
      apiKey: TEST_API_KEY,
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
      apiKey: TEST_API_KEY,
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
    // H-1: API_KEY is per-bot, NOT in systemEnv. Anything per-bot
    // is supplied via BotStartParams.apiKey and applied by buildSpec.
    expect(env.API_KEY).toBeUndefined();
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

  it("forwards DASHBOARD_URL to tenant bots; API_KEY is per-bot (H-1)", () => {
    process.env.API_KEY = "test fixture (ignored by systemEnv)";
    process.env.DASHBOARD_URL = "https://example.com";
    const env = getOrchestratorSystemEnv();
    // H-1: process.env.API_KEY no longer participates in systemEnv.
    expect(env.API_KEY).toBeUndefined();
    expect(env.DASHBOARD_URL).toBe("https://example.com");
  });

  it("includes C-1 operator-policy caps with documented defaults", () => {
    // Clear all relevant overrides so we exercise defaults.
    for (const k of [
      "HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES",
      "HYPERTRADE_BOT_MAX_TOTAL_EXPOSURE_USD",
      "HYPERTRADE_BOT_SIGNAL_SIZE_MAX_MULTIPLIER",
      "HYPERTRADE_BOT_TAKER_FEE_RATE",
      "HYPERTRADE_BOT_TRADE_RATE_ALARM_ENABLED",
      "HYPERTRADE_BOT_TRADE_RATE_ALARM_BASELINE_MULTIPLIER",
      "HYPERTRADE_BOT_TRADE_RATE_ALARM_MIN_HOURLY_FLOOR",
      "HYPERTRADE_BOT_TRADE_RATE_ALARM_ABSOLUTE_CEILING",
      "HYPERTRADE_BOT_TRADE_RATE_ALARM_CHECK_INTERVAL_SECONDS",
      "HYPERTRADE_BOT_HL_READ_TIMEOUT_SECONDS",
      "HYPERTRADE_BOT_HL_ORDER_TIMEOUT_SECONDS",
      "HYPERTRADE_BOT_HL_INIT_RETRY_ATTEMPTS",
      "HYPERTRADE_BOT_HL_INIT_RETRY_BACKOFF_SECONDS",
    ]) {
      delete process.env[k];
    }
    const env = getOrchestratorSystemEnv();
    expect(env.MAINNET_ENABLED_STRATEGIES).toBe("");
    expect(env.MAX_TOTAL_EXPOSURE_USD).toBe("5000");
    expect(env.SIGNAL_SIZE_MAX_MULTIPLIER).toBe("10");
    expect(env.TAKER_FEE_RATE).toBe("0.00045");
    expect(env.TRADE_RATE_ALARM_ENABLED).toBe("true");
    expect(env.TRADE_RATE_ALARM_BASELINE_MULTIPLIER).toBe("5.0");
    expect(env.TRADE_RATE_ALARM_MIN_HOURLY_FLOOR).toBe("5");
    expect(env.TRADE_RATE_ALARM_ABSOLUTE_CEILING).toBe("20");
    expect(env.TRADE_RATE_ALARM_CHECK_INTERVAL_SECONDS).toBe("300");
    expect(env.HL_READ_TIMEOUT_SECONDS).toBe("5.0");
    expect(env.HL_ORDER_TIMEOUT_SECONDS).toBe("15.0");
    expect(env.HL_INIT_RETRY_ATTEMPTS).toBe("5");
    expect(env.HL_INIT_RETRY_BACKOFF_SECONDS).toBe("2.0");
  });

  it("respects HYPERTRADE_BOT_* overrides for the C-1 policy caps", () => {
    process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES = "bb_short,moon_phases";
    process.env.HYPERTRADE_BOT_MAX_TOTAL_EXPOSURE_USD = "12345";
    process.env.HYPERTRADE_BOT_HL_ORDER_TIMEOUT_SECONDS = "30.0";
    const env = getOrchestratorSystemEnv();
    expect(env.MAINNET_ENABLED_STRATEGIES).toBe("bb_short,moon_phases");
    expect(env.MAX_TOTAL_EXPOSURE_USD).toBe("12345");
    expect(env.HL_ORDER_TIMEOUT_SECONDS).toBe("30.0");
  });

  it.each([
    "MAINNET_ENABLED_STRATEGIES",
    "MAX_TOTAL_EXPOSURE_USD",
    "SIGNAL_SIZE_MAX_MULTIPLIER",
    "TAKER_FEE_RATE",
    "TRADE_RATE_ALARM_ENABLED",
    "HL_ORDER_TIMEOUT_SECONDS",
    "MAX_POSITION_SIZE_USD",
  ])(
    "buildSpec: tenant decryptedSecrets cannot override systemEnv key %s",
    (policyKey) => {
      const spec = buildSpec({
        botId: BOT_ID,
        tenantId: TENANT_ID,
        mode: "mainnet",
        decryptedSecrets: { [policyKey]: "TENANT_SPOOF_VALUE" },
        systemEnv: getOrchestratorSystemEnv(),
        apiKey: TEST_API_KEY,
      });
      const entries = spec.env.filter((e) => e.startsWith(`${policyKey}=`));
      expect(entries).toHaveLength(1);
      expect(entries[0]).not.toContain("TENANT_SPOOF_VALUE");
    },
  );

  it("buildSpec — per-bot apiKey wins over decryptedSecrets API_KEY (H-1)", () => {
    const perBotKey = "per-bot-key-abc";
    const spoofKey = "spoof-key";
    const spec = buildSpec({
      botId: BOT_ID,
      tenantId: TENANT_ID,
      mode: "paper",
      decryptedSecrets: { API_KEY: spoofKey },
      systemEnv: getOrchestratorSystemEnv(),
      apiKey: perBotKey,
    });
    const apiKeyEntries = spec.env.filter((e) => e.startsWith("API_KEY="));
    expect(apiKeyEntries).toEqual([`API_KEY=${perBotKey}`]);
  });

  it("buildSpec — per-bot apiKey wins even if systemEnv tries to set API_KEY", () => {
    const perBotKey = "per-bot-key-xyz";
    const spec = buildSpec({
      botId: BOT_ID,
      tenantId: TENANT_ID,
      mode: "paper",
      decryptedSecrets: {},
      systemEnv: { API_KEY: "stale value (test fixture)" },
      apiKey: perBotKey,
    });
    const apiKeyEntries = spec.env.filter((e) => e.startsWith("API_KEY="));
    expect(apiKeyEntries).toEqual([`API_KEY=${perBotKey}`]);
  });
});
