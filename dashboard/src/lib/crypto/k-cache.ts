/**
 * Session-scoped K-cache (multi-tenancy Phase 2c).
 *
 * After a tenant unlocks (verifies their passphrase), we cache the
 * derived 32-byte key K in Redis under a key tied to their session.
 * Subsequent same-session requests can decrypt secrets without
 * re-prompting for the passphrase.
 *
 * Trade-offs (per docs/plans/multi-tenancy.md §4 "Open question —
 * passphrase caching duration"): K lives in Redis with TTL matching
 * the dashboard session. Operator with Redis access could theoretically
 * read K out of Redis — same blast-radius limitation already documented
 * for trust model B in v1 (operator with host root can also read
 * container env vars of running bots). Future hardening: encrypt K
 * at rest in Redis under a server-managed key, or move K-cache into
 * an in-process map and accept that multi-instance dashboards each
 * need their own unlock.
 */

import type { Redis } from "ioredis";

import { getRedisClient } from "../redis";

import { KEY_BYTES } from "./secrets";

/** TTL aligned with dashboard sessions (7 days in lib/auth.ts). */
const DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 7;

function redisKey(tenantId: string, sessionId: string): string {
  return `dashboard:k-cache:${tenantId}:${sessionId}`;
}

/**
 * Cache K for a tenant + session. K is base64-encoded so binary bytes
 * survive Redis's STRING type cleanly. Overwrites any previous K for
 * the same (tenant, session) pair.
 */
export async function cacheKey(
  tenantId: string,
  sessionId: string,
  key: Buffer,
  ttlSeconds: number = DEFAULT_TTL_SECONDS,
  client: Redis = getRedisClient(),
): Promise<void> {
  if (key.length !== KEY_BYTES) {
    throw new RangeError(
      `K must be ${KEY_BYTES} bytes, got ${key.length}`,
    );
  }
  await client.set(
    redisKey(tenantId, sessionId),
    key.toString("base64"),
    "EX",
    ttlSeconds,
  );
}

/**
 * Load K for a tenant + session. Returns null if the cache key is
 * missing (no unlock yet) or expired (session old / explicit clear).
 * Wrong-format values (corrupted Redis row) yield null + a console
 * warning rather than throwing — the API layer can map null → 401
 * "passphrase required" to prompt re-unlock.
 */
export async function loadKey(
  tenantId: string,
  sessionId: string,
  client: Redis = getRedisClient(),
): Promise<Buffer | null> {
  const raw = await client.get(redisKey(tenantId, sessionId));
  if (raw === null) return null;
  let buf: Buffer;
  try {
    buf = Buffer.from(raw, "base64");
  } catch {
    return null;
  }
  if (buf.length !== KEY_BYTES) {
    console.warn(
      `[k-cache] discarding malformed K for ${redisKey(tenantId, sessionId)} (length ${buf.length})`,
    );
    return null;
  }
  return buf;
}

/**
 * Clear K for a tenant + session. Used by /api/tenant/me/lock or on
 * logout. Idempotent — no error if the key is already gone.
 */
export async function clearKey(
  tenantId: string,
  sessionId: string,
  client: Redis = getRedisClient(),
): Promise<void> {
  await client.del(redisKey(tenantId, sessionId));
}
