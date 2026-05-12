/**
 * Dashboard-side TLS-config helper (PR 4a).
 *
 * Reads + writes the `dashboard:tls:*` Redis keys directly,
 * replacing the bot's `/api/tls/config` + `/api/tls/configure`
 * proxy endpoints. Same keys, same env-first override semantics,
 * just one fewer network hop.
 *
 * Same key namespace as `bot/hypertrade/engine/control.py:get_tls_config`
 * — kept in lockstep until PR 4c removes the bot-side handler.
 */

// Server-only — getTlsConfig returns the Cloudflare API token.
// Importing this from a Client Component would bundle it into
// the browser build; `server-only` causes a build error if so.
import "server-only";

import type { Redis } from "ioredis";

import { getRedisClient } from "./redis";

export type TlsConfig = {
  enabled: boolean;
  domain: string;
  email: string;
  cf_token: string;
};

const KEYS = {
  enabled: "dashboard:tls:enabled",
  domain: "dashboard:tls:domain",
  email: "dashboard:tls:email",
  cf_token: "dashboard:tls:cf_token",
} as const;

/**
 * Read the current TLS-config. Env-first; empty env → Redis;
 * empty Redis → safe defaults (disabled, empty fields).
 *
 * Returns the SAME shape as the bot's `/api/tls/config` endpoint
 * so existing callers keep working unchanged.
 */
export async function getTlsConfig(
  client: Redis = getRedisClient(),
): Promise<TlsConfig> {
  const vals = await client.mget(
    KEYS.enabled,
    KEYS.domain,
    KEYS.email,
    KEYS.cf_token,
  );

  const envEnabled = process.env.TLS_ENABLED_ENV || "";
  const envDomain = process.env.TLS_DOMAIN || "";
  const envEmail = process.env.TLS_EMAIL || "";
  const envCfToken = process.env.TLS_CF_API_TOKEN || "";

  const enabled = envEnabled
    ? envEnabled === "1"
    : vals[0] === "1";

  return {
    enabled,
    domain: envDomain || vals[1] || "",
    email: envEmail || vals[2] || "",
    cf_token: envCfToken || vals[3] || "",
  };
}

/**
 * Partial update of tls-config. Same semantics as the bot's
 * POST /api/tls/configure: only keys present in `updates` are
 * touched. `enabled` is always written ("1" or "0") when provided.
 * Empty-string for the other fields = delete the Redis key
 * (env-first fallback or empty default kicks in).
 */
export async function setTlsConfig(
  updates: Partial<TlsConfig>,
  client: Redis = getRedisClient(),
): Promise<void> {
  const pipe = client.pipeline();
  if ("enabled" in updates) {
    pipe.set(KEYS.enabled, updates.enabled ? "1" : "0");
  }
  const stringFields: Array<[keyof TlsConfig, string]> = [
    ["domain", KEYS.domain],
    ["email", KEYS.email],
    ["cf_token", KEYS.cf_token],
  ];
  for (const [arg, key] of stringFields) {
    if (arg in updates) {
      const val = updates[arg];
      if (val === undefined || val === null || val === "") {
        pipe.del(key);
      } else {
        pipe.set(key, val as string);
      }
    }
  }
  await pipe.exec();
}
