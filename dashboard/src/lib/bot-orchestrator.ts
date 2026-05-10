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
};

const IMAGE = process.env.HYPERTRADE_BOT_IMAGE ?? "hypertrade-bot:latest";
const NETWORK = process.env.HYPERTRADE_DOCKER_NETWORK ?? "hypertrade_default";
const DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024;     // 512 MiB
const DEFAULT_NANO_CPUS = 1_000_000_000;            // 1 CPU

/**
 * Container name for a (tenant, mode) pair. Short-id keeps the name
 * within Docker's 63-char limit and human-readable in `docker ps`.
 *   hypertrade-bot-3a2f1e4c-mainnet
 */
export function containerName(tenantId: string, mode: BotMode): string {
  const short = tenantId.replace(/-/g, "").slice(0, 8);
  return `hypertrade-bot-${short}-${mode}`;
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
  const env = [
    `TENANT_ID=${params.tenantId}`,
    `BOT_ID=${params.botId}`,
    `EXCHANGE_MODE=${params.mode}`,
    ...Object.entries(params.decryptedSecrets).map(
      ([k, v]) => `${k}=${v}`,
    ),
  ];
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
