/**
 * Unit tests for the bot orchestrator (multi-tenancy Phase 3a).
 *
 * Pure-function tests for `buildSpec`, `containerName`,
 * `requiredSecretsForMode`, `isValidMode`. Docker calls are mocked
 * via `vi.mock("../docker")` so we exercise the orchestrator's logic
 * without a live Docker socket.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  memoryBytesForMode,
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
      "xupertrade-bot-3a2f1e4caaaabbbb-mainnet",
    );
  });

  it("strips dashes from the tenant uuid", () => {
    const name = containerName("00000000-1111-2222-3333-444444444444", "paper");
    expect(name).toBe("xupertrade-bot-0000000011112222-paper");
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
      mode: "testnet",
      decryptedSecrets: {},
      apiKey: TEST_API_KEY,
    });
    // Non-owner bots stay at 512 MiB; the services owner gets 1 GiB (own
    // describe block below). CPU + restart policy are mode-independent.
    expect(spec.memoryBytes).toBe(512 * 1024 * 1024);
    expect(spec.nanoCpus).toBe(1_000_000_000);
    expect(spec.restartPolicy).toBe("unless-stopped");
    // Log rotation (own describe block below covers all three modes).
    expect(spec.logConfig).toEqual({
      Type: "json-file",
      Config: { "max-size": "50m", "max-file": "5" },
    });
  });

  describe("TENANT_ALLOWED_STRATEGIES / TENANT_KEY_EXPIRIES injection", () => {
    // Regression cover for the fail-open allowlist bug. The bot used to
    // read `tenants.allowed_strategies` and `tenant_secrets.expires_at`
    // straight from Postgres, but its per-tenant PG role has no grant on
    // either table (both are dashboard-owned). Every boot therefore hit
    // InsufficientPrivilegeError; the allowlist read swallowed it in a
    // fail-open `except` and ran with NO tenant filter, and the expiry
    // read logged "Key expiry check failed" daily without ever warning.
    // Both values are now injected here instead.

    it("injects the allowlist as JSON when the tenant has one", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        allowedStrategies: ["bb_short", "hash_momentum"],
      });
      expect(spec.env).toContain(
        'TENANT_ALLOWED_STRATEGIES=["bb_short","hash_momentum"]',
      );
    });

    it("omits the var entirely when the allowlist is null (NULL = no filter)", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        allowedStrategies: null,
      });
      expect(
        spec.env.some((e) => e.startsWith("TENANT_ALLOWED_STRATEGIES=")),
      ).toBe(false);
    });

    it("distinguishes an empty allowlist from an absent one", () => {
      // [] means "zero strategies may trade" and MUST reach the bot as a
      // real value — collapsing it to the null case would silently grant
      // the tenant every strategy.
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        allowedStrategies: [],
      });
      expect(spec.env).toContain("TENANT_ALLOWED_STRATEGIES=[]");
    });

    it("a tenant cannot widen their own allowlist via a smuggled secret", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {
          TENANT_ALLOWED_STRATEGIES: '["everything","i","want"]',
        },
        apiKey: TEST_API_KEY,
        allowedStrategies: ["bb_short"],
      });
      expect(spec.env).toContain('TENANT_ALLOWED_STRATEGIES=["bb_short"]');
      expect(spec.env).not.toContain(
        'TENANT_ALLOWED_STRATEGIES=["everything","i","want"]',
      );
    });

    it("injects the strategy cap when the tenant has one", () => {
      // The toggle route only gates enabling, and a fresh bot boots
      // with every allowlisted strategy on — so without this a tenant
      // capped at 3 ran all of them. The bot trims to the cap at boot.
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "testnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        maxActiveStrategies: 3,
      });
      expect(spec.env).toContain("TENANT_MAX_ACTIVE_STRATEGIES=3");
    });

    it("passes a cap of 0 through rather than dropping it as falsy", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "testnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        maxActiveStrategies: 0,
      });
      expect(spec.env).toContain("TENANT_MAX_ACTIVE_STRATEGIES=0");
    });

    it("omits the cap when null (NULL = no cap)", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "testnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        maxActiveStrategies: null,
      });
      expect(
        spec.env.some((e) => e.startsWith("TENANT_MAX_ACTIVE_STRATEGIES=")),
      ).toBe(false);
    });

    it("a tenant cannot raise their own cap via a smuggled secret", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "testnet",
        decryptedSecrets: { TENANT_MAX_ACTIVE_STRATEGIES: "999" },
        apiKey: TEST_API_KEY,
        maxActiveStrategies: 2,
      });
      expect(spec.env).toContain("TENANT_MAX_ACTIVE_STRATEGIES=2");
      expect(spec.env).not.toContain("TENANT_MAX_ACTIVE_STRATEGIES=999");
    });

    it("injects key expiries as a JSON object", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        keyExpiries: {
          HYPERLIQUID_MAINNET_PRIVATE_KEY: "2026-12-01T00:00:00.000Z",
        },
      });
      expect(spec.env).toContain(
        'TENANT_KEY_EXPIRIES={"HYPERLIQUID_MAINNET_PRIVATE_KEY":"2026-12-01T00:00:00.000Z"}',
      );
    });

    it("omits key expiries when none are set", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        keyExpiries: {},
      });
      expect(spec.env.some((e) => e.startsWith("TENANT_KEY_EXPIRIES="))).toBe(
        false,
      );
    });
  });

  describe("memory cap follows the services owner (vault-scan OOM, 2026-06-17)", () => {
    // The services owner runs the vault scanner + HODL eval + Telegram
    // notifier on top of the strategy loop; pandas/pandas-ta peaks during
    // the daily vault scan OOM-killed it ×4 at a flat 512 MiB cap (then on
    // mainnet, the owner at the time). The owner → 1 GiB; every other bot
    // keeps its proven-stable 512 MiB. nanoCpus is unchanged regardless.
    const MEM_ENV_KEYS = [
      "HYPERTRADE_BOT_MEMORY_BYTES",
      "HYPERTRADE_BOT_PAPER_MEMORY_BYTES",
      "HYPERTRADE_BOT_TESTNET_MEMORY_BYTES",
      "HYPERTRADE_BOT_MAINNET_MEMORY_BYTES",
      "HYPERTRADE_SERVICES_OWNER_MODE",
    ];
    const ORIG = { ...process.env };
    beforeEach(() => {
      for (const k of MEM_ENV_KEYS) delete process.env[k];
    });
    afterEach(() => {
      process.env = { ...ORIG };
    });

    it("gives the default owner (paper) 1 GiB", () => {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "paper",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
      });
      expect(spec.memoryBytes).toBe(1024 * 1024 * 1024);
      // CPU cap is NOT touched by the memory fix.
      expect(spec.nanoCpus).toBe(1_000_000_000);
    });

    it.each(["testnet", "mainnet"] as const)(
      "keeps non-owner %s at 512 MiB",
      (mode) => {
        const spec = buildSpec({
          tenantId: TENANT_ID,
          botId: BOT_ID,
          mode,
          decryptedSecrets: {},
          apiKey: TEST_API_KEY,
        });
        expect(spec.memoryBytes).toBe(512 * 1024 * 1024);
      },
    );

    it("moves the 1 GiB with the owner", () => {
      process.env.HYPERTRADE_SERVICES_OWNER_MODE = "mainnet";
      expect(memoryBytesForMode("mainnet")).toBe(1024 * 1024 * 1024);
      expect(memoryBytesForMode("paper")).toBe(512 * 1024 * 1024);
      expect(memoryBytesForMode("testnet")).toBe(512 * 1024 * 1024);
    });

    it("per-mode env override wins over the default", () => {
      process.env.HYPERTRADE_BOT_MAINNET_MEMORY_BYTES = String(2 * 1024 * 1024 * 1024);
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode: "mainnet",
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
      });
      expect(spec.memoryBytes).toBe(2 * 1024 * 1024 * 1024);
      // Other modes are unaffected by the mainnet-specific override.
      expect(memoryBytesForMode("testnet")).toBe(512 * 1024 * 1024);
    });

    it("global HYPERTRADE_BOT_MEMORY_BYTES applies to modes without a per-mode var", () => {
      process.env.HYPERTRADE_BOT_MEMORY_BYTES = String(700 * 1024 * 1024);
      expect(memoryBytesForMode("paper")).toBe(700 * 1024 * 1024);
      expect(memoryBytesForMode("mainnet")).toBe(700 * 1024 * 1024);
    });

    it("per-mode env override beats the global fallback", () => {
      process.env.HYPERTRADE_BOT_MEMORY_BYTES = String(700 * 1024 * 1024);
      process.env.HYPERTRADE_BOT_MAINNET_MEMORY_BYTES = String(1536 * 1024 * 1024);
      expect(memoryBytesForMode("mainnet")).toBe(1536 * 1024 * 1024);
      expect(memoryBytesForMode("testnet")).toBe(700 * 1024 * 1024);
    });

    it.each(["", "0", "-1", "not-a-number"])(
      "ignores a bogus override value %j and falls back to the default",
      (bad) => {
        process.env.HYPERTRADE_BOT_PAPER_MEMORY_BYTES = bad;
        expect(memoryBytesForMode("paper")).toBe(1024 * 1024 * 1024);
      },
    );
  });

  describe("log rotation (bot/reports/analysis-2026-09-15.md § 1)", () => {
    // Bare json-file (Docker's default) has no size cap: the paper and
    // testnet bot log files reached 404 MB / 379 MB after two weeks in
    // production. Every mode gets the same 50m×5 cap — unlike memory,
    // this isn't mode-dependent.
    it.each(["paper", "testnet", "mainnet"] as const)(
      "caps %s logs at 50m x 5 files",
      (mode) => {
        const spec = buildSpec({
          tenantId: TENANT_ID,
          botId: BOT_ID,
          mode,
          decryptedSecrets: {},
          apiKey: TEST_API_KEY,
        });
        expect(spec.logConfig).toEqual({
          Type: "json-file",
          Config: { "max-size": "50m", "max-file": "5" },
        });
      },
    );
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

  describe("services-owner gate: TELEGRAM_ENABLED + SERVICES_OWNER (NU-7)", () => {
    // Exactly one bot per tenant owns Telegram, HODL, the vault scanner
    // and key reminders: the one whose mode is
    // HYPERTRADE_SERVICES_OWNER_MODE (default paper). Two Telegram
    // pollers on one token collide, and every bot receives every mode's
    // events, so a second owner doubles every notification (the post-PR-4c
    // bug). The gate used to be pinned to mainnet, which meant stopping the
    // real-money bot silenced the alarms.
    const ORIG = { ...process.env };
    beforeEach(() => {
      delete process.env.HYPERTRADE_SERVICES_OWNER_MODE;
    });
    afterEach(() => {
      process.env = { ...ORIG };
    });

    function gate(
      mode: "paper" | "testnet" | "mainnet",
      extra: Partial<Parameters<typeof buildSpec>[0]> = {},
    ) {
      const spec = buildSpec({
        tenantId: TENANT_ID,
        botId: BOT_ID,
        mode,
        decryptedSecrets: {},
        apiKey: TEST_API_KEY,
        ...extra,
      });
      const pick = (k: string) => spec.env.filter((e) => e.startsWith(`${k}=`));
      return {
        telegram: pick("TELEGRAM_ENABLED"),
        owner: pick("SERVICES_OWNER"),
        vault: pick("VAULT_TRACKING_ADDRESS"),
      };
    }

    it("makes paper the owner by default", () => {
      expect(gate("paper")).toMatchObject({
        telegram: ["TELEGRAM_ENABLED=true"],
        owner: ["SERVICES_OWNER=true"],
      });
    });

    it.each(["testnet", "mainnet"] as const)(
      "silences %s by default (mainnet included)",
      (mode) => {
        expect(gate(mode)).toMatchObject({
          telegram: ["TELEGRAM_ENABLED=false"],
          owner: ["SERVICES_OWNER=false"],
        });
      },
    );

    it("follows HYPERTRADE_SERVICES_OWNER_MODE to exactly one mode", () => {
      process.env.HYPERTRADE_SERVICES_OWNER_MODE = "mainnet";
      const owners = (["paper", "testnet", "mainnet"] as const).filter(
        (m) => gate(m).owner[0] === "SERVICES_OWNER=true",
      );
      expect(owners).toEqual(["mainnet"]);
      expect(gate("mainnet").telegram).toEqual(["TELEGRAM_ENABLED=true"]);
      expect(gate("paper").telegram).toEqual(["TELEGRAM_ENABLED=false"]);
    });

    it("falls back to paper on an unknown owner mode, still exactly one owner", () => {
      process.env.HYPERTRADE_SERVICES_OWNER_MODE = "mainet";
      vi.spyOn(console, "warn").mockImplementation(() => {});
      const owners = (["paper", "testnet", "mainnet"] as const).filter(
        (m) => gate(m).owner[0] === "SERVICES_OWNER=true",
      );
      expect(owners).toEqual(["paper"]);
      vi.mocked(console.warn).mockRestore();
    });

    it("a tenant secret cannot flip either flag on a non-owner", () => {
      expect(
        gate("testnet", {
          decryptedSecrets: { TELEGRAM_ENABLED: "true", SERVICES_OWNER: "true" },
        }),
      ).toMatchObject({
        telegram: ["TELEGRAM_ENABLED=false"],
        owner: ["SERVICES_OWNER=false"],
      });
    });

    it("neither a tenant secret nor systemEnv can silence the owner", () => {
      expect(
        gate("paper", {
          decryptedSecrets: { TELEGRAM_ENABLED: "false", SERVICES_OWNER: "false" },
          systemEnv: { TELEGRAM_ENABLED: "false", SERVICES_OWNER: "false" },
        }),
      ).toMatchObject({
        telegram: ["TELEGRAM_ENABLED=true"],
        owner: ["SERVICES_OWNER=true"],
      });
    });

    it("injects the vault tracking address on the owner only", () => {
      const addr = "0x1111111111111111111111111111111111111111";
      expect(gate("paper", { vaultTrackingAddress: addr }).vault).toEqual([
        `VAULT_TRACKING_ADDRESS=${addr}`,
      ]);
      expect(gate("mainnet", { vaultTrackingAddress: addr }).vault).toEqual([]);
    });

    it("the Phase address wins over the tenant's slot on the owner", () => {
      const phase = "0x1111111111111111111111111111111111111111";
      const tenantSlot = "0x2222222222222222222222222222222222222222";
      expect(
        gate("paper", {
          decryptedSecrets: { VAULT_TRACKING_ADDRESS: tenantSlot },
          vaultTrackingAddress: phase,
        }).vault,
      ).toEqual([`VAULT_TRACKING_ADDRESS=${phase}`]);
    });

    it("keeps the tenant's own slot when no Phase address is passed", () => {
      const tenantSlot = "0x2222222222222222222222222222222222222222";
      expect(
        gate("paper", {
          decryptedSecrets: { VAULT_TRACKING_ADDRESS: tenantSlot },
          vaultTrackingAddress: null,
        }).vault,
      ).toEqual([`VAULT_TRACKING_ADDRESS=${tenantSlot}`]);
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
      name: "xupertrade-bot-3a2f1e4c-paper",
      image: "xupertrade-bot:latest",
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
    expect(passedSpec.name).toBe("xupertrade-bot-3a2f1e4caaaabbbb-paper");
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
