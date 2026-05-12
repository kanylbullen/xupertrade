/**
 * Rate-limit helper (PR 3d).
 *
 * Fixed-window counter in Redis: increment a counter keyed
 * `ratelimit:<scope>:<bucket>` with a TTL = window length. When the
 * count exceeds `max`, deny. Simple, race-safe (INCR is atomic), and
 * the window naturally rolls when the key expires.
 *
 * Trade-off vs. token bucket: a flood at the boundary can fire up
 * to 2x in <window seconds (last hit of window N + first of N+1).
 * Acceptable here — these limits are for human-paced actions
 * (sending an unlock-link DM, attempting passphrase), not API
 * throughput.
 */

import type { Redis } from "ioredis";

import { getRedisClient } from "./redis";

export type RateLimitResult =
  | { allowed: true; remaining: number; resetInSeconds: number }
  | { allowed: false; remaining: 0; resetInSeconds: number };

/**
 * Check + increment a rate-limit counter. Returns whether the
 * action is allowed and how long until the window rolls.
 *
 * @param scope — caller-defined namespace (e.g. "unlock-link-send").
 * @param bucket — the thing being rate-limited (e.g. tenant_id).
 *                  Different tenants don't share the same counter.
 * @param max — max events per window. Inclusive (max=5 allows 5
 *              events before the 6th is denied).
 * @param windowSeconds — window length.
 */
export async function checkRateLimit(
  scope: string,
  bucket: string,
  max: number,
  windowSeconds: number,
  client: Redis = getRedisClient(),
): Promise<RateLimitResult> {
  const key = `ratelimit:${scope}:${bucket}`;
  // INCR creates the key if absent (with value 1); EXPIRE in the
  // same pipeline sets the TTL only on the first hit of a window
  // (we use NX to avoid resetting the window mid-flight).
  const pipeline = client.multi();
  pipeline.incr(key);
  pipeline.expire(key, windowSeconds, "NX");
  pipeline.ttl(key);
  const results = await pipeline.exec();
  if (!results) {
    // Redis pipeline error — fail open. Logging the failure is
    // the caller's responsibility if they care.
    return { allowed: true, remaining: max, resetInSeconds: windowSeconds };
  }

  // pipeline.exec() returns [err, value] tuples per command. A
  // single command can fail (Redis cluster slot bounce, OOM
  // eviction race, etc.) while others succeed. Validate that
  // both the INCR result + the TTL result are real numbers; if
  // anything's off, fail open with sane defaults.
  function asNumber(entry: unknown, fallback: number): number {
    if (!Array.isArray(entry)) return fallback;
    const [err, val] = entry;
    if (err !== null) return fallback;
    return typeof val === "number" && Number.isFinite(val) ? val : fallback;
  }
  const count = asNumber(results[0], 0);
  const rawTtl = asNumber(results[2], windowSeconds);
  // TTL can be -1 (key has no expiry — shouldn't happen via INCR
  // + EXPIRE NX, but defense) or -2 (key vanished between INCR
  // and TTL, also shouldn't happen but Redis docs say it can).
  // Clamp to windowSeconds in those cases so Retry-After stays
  // useful and non-negative.
  const ttl = rawTtl > 0 ? rawTtl : windowSeconds;
  const remaining = Math.max(0, max - count);
  if (count > max) {
    return { allowed: false, remaining: 0, resetInSeconds: ttl };
  }
  return { allowed: true, remaining, resetInSeconds: ttl };
}
