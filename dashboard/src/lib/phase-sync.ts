/**
 * Phase → Redis auth-config sync (PR feat/phase-auth-autosync).
 *
 * Triggered from `src/instrumentation.ts` at every dashboard container
 * start. Reads operator-managed env vars (injected by `phase run` in the
 * compose entrypoint) and writes them into the same `dashboard:auth:*`
 * Redis keys that `lib/auth-config.ts` reads.
 *
 * Why this exists: production incident 2026-05-13 — operator rotated
 * their Authentik OIDC slug, updated Phase, but Redis still held the
 * old value. `getAuthConfig` env-first override masked the bug for some
 * paths, but the OIDC callback path read straight from Redis and broke
 * with OAUTH_RESPONSE_IS_NOT_CONFORM. Forcing Redis to mirror Phase on
 * every boot eliminates that drift class entirely.
 *
 * Design choice A1 (always overwrite): Phase is source of truth. UI
 * Settings → Authentication remains writable, but operator should know
 * edits don't survive restart. Banner in `auth-config.tsx` flags this
 * when any of the env vars is non-empty.
 *
 * Server-only — uses ioredis. Must not be bundled into a Client
 * Component build (the instrumentation hook guards this via the
 * `process.env.NEXT_RUNTIME === "nodejs"` fence).
 */
import "server-only";

import type { Redis } from "ioredis";

import { getRedisClient } from "./redis";

/** Env-var → Redis-key mapping. Order matters only for the log. */
const SYNC_KEYS: Array<{ env: string; redis: string }> = [
  { env: "OIDC_ISSUER", redis: "dashboard:auth:oidc:issuer" },
  { env: "OIDC_CLIENT_ID", redis: "dashboard:auth:oidc:client_id" },
  { env: "OIDC_CLIENT_SECRET", redis: "dashboard:auth:oidc:client_secret" },
  { env: "OIDC_SCOPES", redis: "dashboard:auth:oidc:scopes" },
  { env: "AUTH_MODE", redis: "dashboard:auth:mode" },
];

export type PhaseSyncResult = {
  /** How many env vars were non-empty and got written. */
  written: number;
  /** Total number of env vars we look at (5 today). */
  total: number;
  /** True if Redis was unreachable; no writes happened. */
  redisError: boolean;
};

/**
 * Sync Phase-injected env vars into Redis. Idempotent, safe to call
 * concurrently — last write wins per key. Does NOT propagate Redis
 * errors; logs at WARN and returns instead, so a transient Redis
 * outage doesn't crash dashboard startup.
 */
export async function syncPhaseAuthConfig(
  client: Redis = getRedisClient(),
): Promise<PhaseSyncResult> {
  const total = SYNC_KEYS.length;
  const present = SYNC_KEYS
    .map(({ env, redis }) => {
      const raw = process.env[env];
      const trimmed = raw == null ? "" : String(raw).trim();
      return { redis, value: trimmed };
    })
    .filter((x) => x.value !== "");

  if (present.length === 0) {
    // Nothing to do — Phase isn't injecting any of these. Keep whatever
    // Redis already holds (typically operator-typed via the UI).
    console.log(
      `[phase-sync] no OIDC env vars set, skipped (0/${total} keys)`,
    );
    return { written: 0, total, redisError: false };
  }

  try {
    const pipe = client.pipeline();
    for (const { redis, value } of present) {
      pipe.set(redis, value);
    }
    await pipe.exec();
    console.log(
      `[phase-sync] OIDC config synced from env (${present.length}/${total} keys)`,
    );
    return { written: present.length, total, redisError: false };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(
      `[phase-sync] Redis unreachable, sync skipped — getAuthConfig will fall back to env-first: ${msg}`,
    );
    return { written: 0, total, redisError: true };
  }
}

/**
 * Server-side helper for the UI banner. Returns true if ANY of the
 * sync'd env vars is non-empty after trim — i.e. Phase IS the source
 * of truth and UI edits will be overwritten on next restart.
 */
export function isPhaseManagingAuth(): boolean {
  return SYNC_KEYS.some(({ env }) => {
    const raw = process.env[env];
    return raw != null && String(raw).trim() !== "";
  });
}
