/**
 * Server-side session revocation list (security audit H-3).
 *
 * Sessions are HMAC-signed JWT-style cookies (see `lib/auth.ts`) and
 * have no per-session server record. That means "logout" can only ask
 * the browser to drop its cookie — anyone who exfiltrated the value
 * (XSS, clipboard sniffer, shoulder-surf, malicious extension) can
 * keep using it until the 7-day exp regardless of logout.
 *
 * This module adds a minimal Redis-backed revocation list to defeat
 * that: logout writes `session:revoked:<sha256(cookie)>`; the verify
 * path checks the same key and treats hits as "no session". One
 * Redis GET per authenticated request — fast enough to sit on the
 * hot path.
 *
 * Fail-closed on Redis error: matches the existing convention
 * (`fetchAuthConfig` and the C-1 / H-2 fixes all fail closed). A
 * transient Redis outage should bounce users to /login, not silently
 * disable the revocation check.
 */

import "server-only";

import { createHash } from "node:crypto";

import type { Redis } from "ioredis";

import { getRedisClient } from "./redis";

/**
 * 8 days — comfortably covers the 7-day session exp so a revoked
 * cookie can never come back to life. Per-entry TTL prevents Redis
 * from accumulating dead revocations forever; we don't need to
 * remember a session beyond its exp because the HMAC check will
 * already reject expired cookies.
 */
const REVOCATION_TTL_SECONDS = 60 * 60 * 24 * 8;

function revocationKey(cookieValue: string): string {
  const hash = createHash("sha256").update(cookieValue).digest("hex");
  return `session:revoked:${hash}`;
}

/**
 * Mark a session cookie as revoked. Idempotent — repeated calls just
 * refresh the TTL. Safe to pass an empty/garbage cookie (no-op).
 */
export async function markSessionRevoked(
  cookieValue: string,
  client: Redis = getRedisClient(),
  ttlSeconds: number = REVOCATION_TTL_SECONDS,
): Promise<void> {
  if (!cookieValue) return;
  await client.set(revocationKey(cookieValue), "1", "EX", ttlSeconds);
}

/**
 * Check whether a cookie has been revoked. Returns true (treat as
 * revoked) on Redis error to fail closed. Returns false for empty
 * input — verifySession will reject those upstream anyway, no need
 * to round-trip Redis.
 */
export async function isSessionRevoked(
  cookieValue: string,
  client: Redis = getRedisClient(),
): Promise<boolean> {
  if (!cookieValue) return false;
  try {
    const v = await client.get(revocationKey(cookieValue));
    return v !== null;
  } catch {
    // Fail closed: Redis hiccup must not let a revoked cookie sneak
    // through. Caller treats `true` as "no session" → user is sent
    // back to login.
    return true;
  }
}
