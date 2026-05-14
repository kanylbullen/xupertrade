/**
 * Bot orchestrator — multi-tenancy Phase 3a.
 *
 * Translates a `tenant_bots` row + a tenant's decrypted secrets into
 * a Docker container spec, then drives create/start/stop/remove via
 * `lib/docker.ts`. The DB rows are the source of truth for "what
 * SHOULD be running"; the Docker daemon is the source of truth for
 * "what IS running". A future reconcile pass (post-Phase 3a) will
 * close the gap.
 *
 * Trust model B caveat (per docs/plans/multi-tenancy.md §4): the
 * container's env vars contain the decrypted secrets in plaintext.
 * Operator with `docker inspect` access can read them. v1 limitation;
 * v2 hardening swaps to tmpfs-mount injection.
 */

import {
  type ContainerInfo,
  type ContainerSpec,
  createAndStart,
  inspectContainer,
  stopAndRemove,
} from "./docker";

export type BotMode = "paper" | "testnet" | "mainnet";

const VALID_MODES: readonly BotMode[] = ["paper", "testnet", "mainnet"] as const;

export function isValidMode(s: unknown): s is BotMode {
  return typeof s === "string" && (VALID_MODES as readonly string[]).includes(s);
}

/**
 * Bot HTTP API port per mode. Operator's compose-defined bots already
 * use these (paper=8000, testnet=8001, mainnet=8002 set via the
 * environment block in docker-compose.yml). Per-tenant orchestrator-
 * spawned bots inherit the same convention via API_PORT in
 * `buildSpec` so a single getBotApiUrl helper works for both.
 */
export const API_PORT_BY_MODE: Readonly<Record<BotMode, number>> = {
  paper: 8000,
  testnet: 8001,
  mainnet: 8002,
};

export type BotStartParams = {
  /** UUID of the tenant_bots row. */
  botId: string;
  /** UUID of the owning tenant. */
  tenantId: string;
  mode: BotMode;
  /**
   * Plaintext secret values to inject as env vars. Keys must already
   * be the env-var names the bot expects (e.g.
   * `HYPERLIQUID_PRIVATE_KEY`). Caller is responsible for decrypting
   * via `crypto/secrets.ts:decryptSecret` and not logging anything.
   */
  decryptedSecrets: Record<string, string>;
  /**
   * System-managed env vars (orchestrator-supplied, not
   * user-supplied). e.g. `DATABASE_URL` with the tenant's PG role
   * credentials (Phase 5b). Distinguished from `decryptedSecrets`
   * so it's clear the user can't override these via the secret CRUD
   * API. Merged into the env list AFTER decryptedSecrets so system
   * vars win on collision.
   */
  systemEnv?: Record<string, string>;
};

const IMAGE = process.env.HYPERTRADE_BOT_IMAGE ?? "xupertrade-bot:latest";
const NETWORK = process.env.HYPERTRADE_DOCKER_NETWORK ?? "hypertrade_default";
const DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024;     // 512 MiB
const DEFAULT_NANO_CPUS = 1_000_000_000;            // 1 CPU

/**
 * Container name for a (tenant, mode) pair. We use 16 hex chars
 * (64 bits) of the tenant UUID to make accidental cross-tenant
 * collisions effectively impossible — 8 chars (32 bits) is too
 * short, full 32 chars is overkill. Total name length:
 *   "xupertrade-bot-" (15) + 16 + "-" (1) + "mainnet" (7) = 39
 * comfortably under Docker's 63-char limit.
 *   xupertrade-bot-3a2f1e4caaaa1111-mainnet
 */
export function containerName(tenantId: string, mode: BotMode): string {
  const short = tenantId.replace(/-/g, "").slice(0, 16);
  return `xupertrade-bot-${short}-${mode}`;
}

/**
 * Compose-defined env vars the bot needs but the orchestrator's
 * caller doesn't supply per-tenant. Mirrors `x-bot-env` in
 * docker-compose.yml — without these the bot crashes at startup
 * (e.g. tries to connect to localhost:6379 instead of the in-network
 * `redis:6379`, then docker `restart: unless-stopped` retries
 * forever, spamming any configured Telegram with `Xupertrade
 * started` messages on each retry).
 *
 * Defaults match compose 1:1 for the single-host operator
 * deployment. Override via `HYPERTRADE_BOT_*` env on the dashboard
 * service for non-default deployments.
 *
 * **API_KEY is critical**: it gates the bot's `/strategies` and
 * other auth-required endpoints. We inject the dashboard's own
 * `API_KEY` (the same one the dashboard forwards as `X-Api-Key`
 * when proxying to the bot) so bot↔dashboard auth round-trips
 * work. Because `buildSpec`'s envMap puts `systemEnv` AFTER
 * `decryptedSecrets`, this also prevents a malicious tenant from
 * setting a bogus `API_KEY` via the secret CRUD API to escape the
 * dashboard's auth gate.
 *
 * **Operator-policy env vars (security audit C-1, 2026-05-12).**
 * Every env var below this comment is here specifically so that even
 * if the secret-CRUD allowlist is loosened or bypassed, the
 * orchestrator's value wins (buildSpec puts systemEnv AFTER
 * decryptedSecrets). These caps and timeouts encode operator policy
 * — not tenant preference — so they must NOT be tenant-overridable.
 *
 * **What's intentionally NOT here**:
 *   - DATABASE_URL — caller-supplied per-tenant (`tenantDbUrl`)
 *     so each tenant connects under its own Postgres role for
 *     RLS isolation (Phase 5b).
 *   - TENANT_ID, BOT_ID, EXCHANGE_MODE, API_PORT — set by
 *     `buildSpec` directly (stable per-bot identifiers).
 *   - Per-tenant credential secrets via `decryptedSecrets` — exact set
 *     gated by `TENANT_ALLOWED_SECRETS` in
 *     `app/api/tenant/me/secrets/[key]/route.ts`. Currently:
 *     `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_ACCOUNT_ADDRESS`,
 *     `HYPERLIQUID_MAINNET_PRIVATE_KEY`,
 *     `HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS`, `TELEGRAM_BOT_TOKEN`,
 *     `TELEGRAM_CHAT_ID`, `VAULT_TRACKING_ADDRESS`. Anything outside
 *     that set is rejected at write time, so it can't end up here.
 *   - AUTH_MODE, OIDC_*, TLS_* — owned exclusively by the dashboard
 *     (read directly from Redis via `lib/auth-config.ts` +
 *     `lib/tls-config.ts`). Bots have nothing to do with auth/TLS config.
 */
export function getOrchestratorSystemEnv(): Record<string, string> {
  return {
    REDIS_URL: process.env.HYPERTRADE_BOT_REDIS_URL ?? "redis://redis:6379/0",
    PAPER_INITIAL_BALANCE:
      process.env.HYPERTRADE_BOT_PAPER_INITIAL_BALANCE ?? "10000",
    POLL_INTERVAL_SECONDS:
      process.env.HYPERTRADE_BOT_POLL_INTERVAL_SECONDS ?? "60",
    MAX_POSITION_SIZE_USD:
      process.env.HYPERTRADE_BOT_MAX_POSITION_SIZE_USD ?? "200",
    MAX_DAILY_LOSS_USD:
      process.env.HYPERTRADE_BOT_MAX_DAILY_LOSS_USD ?? "100",
    KILL_SWITCH: process.env.HYPERTRADE_BOT_KILL_SWITCH ?? "false",
    DASHBOARD_URL:
      process.env.DASHBOARD_URL ?? "http://localhost:3000",
    // API_KEY is shared bot↔dashboard secret. Empty when not set
    // → bot endpoints are unauthenticated (current operator
    // default for this hobby deployment). buildSpec puts systemEnv
    // AFTER decryptedSecrets so a tenant can't smuggle their own
    // API_KEY in via secret CRUD to bypass auth.
    API_KEY: process.env.API_KEY ?? "",
    // --- C-1 operator-policy caps (must not be tenant-overridable) ---
    // Empty default = audit-C3 fail-closed allowlist (no strategies
    // allowed on mainnet unless operator explicitly opts in).
    MAINNET_ENABLED_STRATEGIES:
      process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES ?? "",
    MAX_TOTAL_EXPOSURE_USD:
      process.env.HYPERTRADE_BOT_MAX_TOTAL_EXPOSURE_USD ?? "5000",
    SIGNAL_SIZE_MAX_MULTIPLIER:
      process.env.HYPERTRADE_BOT_SIGNAL_SIZE_MAX_MULTIPLIER ?? "10",
    TAKER_FEE_RATE: process.env.HYPERTRADE_BOT_TAKER_FEE_RATE ?? "0.00045",
    TRADE_RATE_ALARM_ENABLED:
      process.env.HYPERTRADE_BOT_TRADE_RATE_ALARM_ENABLED ?? "true",
    TRADE_RATE_ALARM_BASELINE_MULTIPLIER:
      process.env.HYPERTRADE_BOT_TRADE_RATE_ALARM_BASELINE_MULTIPLIER ?? "5.0",
    TRADE_RATE_ALARM_MIN_HOURLY_FLOOR:
      process.env.HYPERTRADE_BOT_TRADE_RATE_ALARM_MIN_HOURLY_FLOOR ?? "5",
    TRADE_RATE_ALARM_ABSOLUTE_CEILING:
      process.env.HYPERTRADE_BOT_TRADE_RATE_ALARM_ABSOLUTE_CEILING ?? "20",
    TRADE_RATE_ALARM_CHECK_INTERVAL_SECONDS:
      process.env.HYPERTRADE_BOT_TRADE_RATE_ALARM_CHECK_INTERVAL_SECONDS ??
      "300",
    HL_READ_TIMEOUT_SECONDS:
      process.env.HYPERTRADE_BOT_HL_READ_TIMEOUT_SECONDS ?? "5.0",
    HL_ORDER_TIMEOUT_SECONDS:
      process.env.HYPERTRADE_BOT_HL_ORDER_TIMEOUT_SECONDS ?? "15.0",
    HL_INIT_RETRY_ATTEMPTS:
      process.env.HYPERTRADE_BOT_HL_INIT_RETRY_ATTEMPTS ?? "5",
    HL_INIT_RETRY_BACKOFF_SECONDS:
      process.env.HYPERTRADE_BOT_HL_INIT_RETRY_BACKOFF_SECONDS ?? "2.0",
  };
}

/** Required secret keys per mode. Used to validate the bot has all
 *  it needs BEFORE we try to start the container (cleaner UX than
 *  crashing the bot at HL-init time). */
export function requiredSecretsForMode(mode: BotMode): string[] {
  switch (mode) {
    case "paper":
      return [];   // paper exchange is in-memory; no creds needed
    case "testnet":
    case "mainnet":
      return ["HYPERLIQUID_PRIVATE_KEY"];
    // HYPERLIQUID_ACCOUNT_ADDRESS is optional (API-wallet pattern).
    // TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are optional.
  }
}

/**
 * Build the container spec — pure function, easy to test.
 */
export function buildSpec(params: BotStartParams): ContainerSpec {
  // Build a single key→value map so each env var has exactly one
  // entry. POSIX allows duplicate `KEY=value` entries in a
  // process's env array but `getenv()` behaviour is implementation-
  // defined (PR #46 review fix) — relying on "last wins" was a
  // portability footgun. Order of overrides:
  //   1. fixed system identifiers (TENANT_ID, BOT_ID, EXCHANGE_MODE)
  //   2. decryptedSecrets (user-supplied)
  //   3. systemEnv (orchestrator-supplied; wins over user)
  //   4. API_PORT (mode-pinned)
  //   5. TELEGRAM_ENABLED (mode-pinned; PR #99)
  // Steps 4 and 5 are mode-derived final overrides — they're set
  // explicitly AFTER the spread so neither a tenant-supplied secret
  // nor a stale orchestrator-system value can win over them. API_PORT
  // pins the routing convention (bots must listen on the port their
  // mode dictates: paper=8000, testnet=8001, mainnet=8002 — so a single
  // getBotApiUrl helper in lib/bot-api.ts works for everything).
  // TELEGRAM_ENABLED pins the single-Telegram-owner convention (only
  // mainnet posts; see the comment on that key below).
  const envMap: Record<string, string> = {
    TENANT_ID: params.tenantId,
    BOT_ID: params.botId,
    EXCHANGE_MODE: params.mode,
    ...params.decryptedSecrets,
    ...(params.systemEnv ?? {}),
    API_PORT: String(API_PORT_BY_MODE[params.mode]),
    // Mode-gate Telegram so only ONE bot per tenant posts notifications.
    // The legacy compose model hardcoded `TELEGRAM_ENABLED=false` on
    // bot-paper and bot-mainnet; only bot-testnet posted (and
    // subscribed to all 3 modes' event channels for routing). After
    // PR 4c retired the compose-bot model, every orchestrator-spawned
    // bot inherited `Settings.telegram_enabled=True` (the bot config
    // default) — so paper + testnet both fired notifiers and the
    // operator saw EVERY trade.executed twice (once paper-tagged,
    // once testnet-tagged).
    //
    // 2026-05-13: ownership moved from testnet to mainnet. Corollary
    // to PR #113, which moved the vault scanner to mainnet (vaults
    // only exist on HL mainnet). Mainnet is now the single owner of
    // both Telegram notifications and the vault scanner, so the
    // operator sees the real-money side prefixed correctly instead
    // of every notification tagged TESTNET.
    //   - mainnet is the canonical Telegram owner (matches vault
    //     scanner ownership; its notifier subscribes to
    //     paper+testnet+mainnet event channels for cross-mode
    //     routing).
    //   - testnet is silenced (was the previous owner; would
    //     otherwise double-emit during the overlap window — operator
    //     must restart both bots after deploy).
    //   - paper is silenced (would spam duplicates of every
    //     trade.executed since all bots' notifiers receive the same
    //     pubsub messages).
    // Placed AFTER the `...systemEnv` spread (and AFTER
    // `...decryptedSecrets`) so neither tenant-supplied
    // TELEGRAM_ENABLED nor a stale orchestrator-system value can win
    // over the mode-derived gate. Same pattern as API_PORT — the
    // value is mode-pinned and not operator-overridable per-bot.
    TELEGRAM_ENABLED: params.mode === "mainnet" ? "true" : "false",
  };
  const env = Object.entries(envMap).map(([k, v]) => `${k}=${v}`);
  return {
    name: containerName(params.tenantId, params.mode),
    image: IMAGE,
    env,
    networkName: NETWORK,
    memoryBytes: DEFAULT_MEMORY_BYTES,
    nanoCpus: DEFAULT_NANO_CPUS,
    restartPolicy: "unless-stopped",
    labels: {
      "hypertrade.tenant_id": params.tenantId,
      "hypertrade.bot_id": params.botId,
      "hypertrade.mode": params.mode,
    },
  };
}

/**
 * Start a tenant's bot. Returns the live container info.
 * Caller (API route) is responsible for:
 *   - validating multi_bot_enabled + existing-count gate
 *   - validating required secrets are present
 *   - persisting `container_id` + `container_name` + `is_running` on
 *     the tenant_bots row after this returns
 */
export async function startBot(params: BotStartParams): Promise<ContainerInfo> {
  return createAndStart(buildSpec(params));
}

/**
 * Stop + remove a tenant's bot. Idempotent on already-gone.
 * Caller is responsible for clearing `container_id` and setting
 * `is_running=false` on the tenant_bots row.
 */
export async function stopBot(containerId: string): Promise<void> {
  return stopAndRemove(containerId);
}

/**
 * Re-inspect a known container by id. Returns null if it's been
 * removed out from under us (e.g. operator did `docker rm` manually).
 */
export async function statusBot(
  containerId: string,
): Promise<ContainerInfo | null> {
  try {
    return await inspectContainer(containerId);
  } catch (err) {
    // 404 = container gone; treat as null so caller can mark
    // is_running=false in the DB.
    if (
      typeof err === "object" &&
      err !== null &&
      "statusCode" in err &&
      (err as { statusCode: number }).statusCode === 404
    ) {
      return null;
    }
    throw err;
  }
}
