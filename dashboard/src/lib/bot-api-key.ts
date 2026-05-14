/**
 * Per-tenant-bot API key store (security audit H-1).
 *
 * Pre-fix: every per-tenant bot received the dashboard's single
 * shared `process.env.API_KEY`. Any tenant who learned that value (or
 * any operator with read access to one tenant's container env) could
 * call ANY OTHER tenant's bot HTTP API directly inside the docker
 * network. Audit prio High.
 *
 * Post-fix: each tenant bot gets a unique random 32-byte URL-safe
 * base64 key generated at start time. The key is persisted in Redis
 * keyed by botId (already a globally-unique UUID — no tenant prefix
 * needed) so the dashboard can look it up when proxying requests.
 * Knowing one bot's key gives access to that bot only.
 *
 * Lifecycle:
 *   - Start  → generate + persist + inject into container env
 *   - Stop   → DEL the key (drop in-process cache too)
 *   - Delete → DEL the key (idempotent)
 *   - Restart (Stop → Start) generates a fresh key.
 *
 * The dashboard's own `process.env.API_KEY` (if set) is no longer
 * injected into tenant bots, but is still used by the small number of
 * legacy auth-bridge call sites that talk to the operator-set bot
 * (e.g. session-secret fetch). Those call sites are being migrated to
 * `loadBotApiKey` as part of the same fix.
 */

import { randomBytes } from "node:crypto";

import type { Redis } from "ioredis";

import { getRedisClient } from "./redis";

const CACHE_TTL_MS = 60_000;

type CacheEntry = { value: string | null; expiresAt: number };
const cache = new Map<string, CacheEntry>();

function redisKey(botId: string): string {
  return `tenant:bot:${botId}:api_key`;
}

/**
 * Generate a new random API key. 32 bytes of entropy encoded as
 * URL-safe base64 (43 chars, no padding). Caller is responsible for
 * persisting the result via `persistBotApiKey`.
 */
export function generateBotApiKey(): string {
  return randomBytes(32).toString("base64url");
}

export async function persistBotApiKey(
  botId: string,
  key: string,
  client: Redis = getRedisClient(),
): Promise<void> {
  await client.set(redisKey(botId), key);
  // Refresh in-process cache so a load right after persist sees the
  // new value without a Redis round-trip.
  cache.set(botId, { value: key, expiresAt: Date.now() + CACHE_TTL_MS });
}

/**
 * Look up the API key for a bot. Returns null if the bot has no
 * key (either never started under the per-bot model, or already
 * stopped/deleted). Cached in-process for 60s to keep the hot proxy
 * path off Redis.
 */
export async function loadBotApiKey(
  botId: string,
  client: Redis = getRedisClient(),
): Promise<string | null> {
  const now = Date.now();
  const hit = cache.get(botId);
  if (hit && hit.expiresAt > now) return hit.value;

  const value = await client.get(redisKey(botId));
  cache.set(botId, { value, expiresAt: now + CACHE_TTL_MS });
  return value;
}

export async function clearBotApiKey(
  botId: string,
  client: Redis = getRedisClient(),
): Promise<void> {
  await client.del(redisKey(botId));
  cache.delete(botId);
}

/** Test-only: drop the in-process cache. Not exported for prod use. */
export function _resetBotApiKeyCacheForTests(): void {
  cache.clear();
}
