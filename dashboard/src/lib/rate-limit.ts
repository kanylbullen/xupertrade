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
 * Scopes that DENY when the counter can't be read, instead of the
 * default allow.
 *
 * analysis-2026-09-15 § 5, Low. Failing open is the right default for
 * a rate limit: it is a nuisance control, and a Redis hiccup should
 * not lock out legitimate users. `tenant-unlock` is not that. It is
 * the only thing standing in front of an Argon2id derivation that
 * decrypts the tenant's HyperLiquid key, so its failure mode is
 * "unlimited offline-speed passphrase guessing", and an attacker who
 * can make the pipeline fail — memory pressure, an eviction race, a
 * cluster slot bounce — gets exactly that by making it fail. A locked
 * -out tenant retries in a minute; a guessed passphrase is permanent.
 *
 * Add to this set only where the thing being limited is expensive or
 * irreversible, not merely annoying.
 */
const FAIL_CLOSED_SCOPES = new Set(["tenant-unlock"]);

function unreadable(
  scope: string,
  max: number,
  windowSeconds: number,
): RateLimitResult {
  if (FAIL_CLOSED_SCOPES.has(scope)) {
    return { allowed: false, remaining: 0, resetInSeconds: windowSeconds };
  }
  return { allowed: true, remaining: max, resetInSeconds: windowSeconds };
}

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

  let results: Awaited<ReturnType<typeof pipeline.exec>>;
  try {
    results = await pipeline.exec();
  } catch {
    // Connection refused, timeout, protocol error. Previously this
    // propagated and the route 500'd; route it through the same
    // policy as every other unreadable-counter case instead.
    return unreadable(scope, max, windowSeconds);
  }
  if (!results) {
    return unreadable(scope, max, windowSeconds);
  }

  // pipeline.exec() returns [err, value] tuples per command. A
  // single command can fail (Redis cluster slot bounce, OOM
  // eviction race, etc.) while others succeed.
  function asNumber(entry: unknown): number | null {
    if (!Array.isArray(entry)) return null;
    const [err, val] = entry;
    if (err !== null) return null;
    return typeof val === "number" && Number.isFinite(val) ? val : null;
  }

  // The INCR result is the counter. Without it we do not know how
  // many attempts have been made, which is the whole answer — so
  // this is an unreadable counter, not a count of zero. Treating it
  // as zero is what made the unlock bucket fail open.
  const rawCount = asNumber(results[0]);
  if (rawCount === null) {
    return unreadable(scope, max, windowSeconds);
  }
  const count = rawCount;
  // The TTL is only cosmetic (Retry-After), so a bad read there
  // falls back to the nominal window in every scope.
  const rawTtl = asNumber(results[2]) ?? windowSeconds;
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
