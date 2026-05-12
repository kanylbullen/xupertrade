/**
 * Dashboard-side auth-config helper (PR 4a).
 *
 * Reads + writes the `dashboard:auth:*` Redis keys directly,
 * replacing the bot's `/api/auth/config` + `/api/auth/configure`
 * proxy endpoints. The bot was just a thin wrapper over the same
 * Redis keys this module now hits — no behavior change for the
 * dashboard, just one fewer network hop.
 *
 * Env-first override stays. When the operator sets these in
 * Phase, env wins over Redis. Empty env values fall back to
 * Redis for back-compat with the old Settings UI flow.
 *
 * Same key namespace as `bot/hypertrade/engine/control.py:get_auth_config`
 * — kept in lockstep until PR 4c removes the bot-side handler.
 */

// Server-only — getAuthConfig returns OIDC client_secret +
// basic_hash + session_secret. Importing this from a Client
// Component would bundle those secrets into the browser build.
// `server-only` causes a build error if that happens.
import "server-only";

import type { Redis } from "ioredis";
import { randomBytes } from "node:crypto";

import { getRedisClient } from "./redis";

export type AuthMode = "disabled" | "basic" | "oidc";

export type AuthConfig = {
  mode: AuthMode;
  basic_user: string;
  basic_hash: string;
  session_secret: string;
  oidc_issuer: string;
  oidc_client_id: string;
  oidc_client_secret: string;
  oidc_scopes: string;
};

const KEYS = {
  mode: "dashboard:auth:mode",
  basic_user: "dashboard:auth:basic:user",
  basic_hash: "dashboard:auth:basic:hash",
  session_secret: "dashboard:auth:session_secret",
  oidc_issuer: "dashboard:auth:oidc:issuer",
  oidc_client_id: "dashboard:auth:oidc:client_id",
  oidc_client_secret: "dashboard:auth:oidc:client_secret",
  oidc_scopes: "dashboard:auth:oidc:scopes",
} as const;

const DEFAULT_OIDC_SCOPES = "openid profile email";

function isValidMode(v: string | null | undefined): v is AuthMode {
  return v === "disabled" || v === "basic" || v === "oidc";
}

/**
 * Read the current auth-config. Env-first; empty env → Redis;
 * empty Redis → safe defaults ("disabled" mode, empty fields).
 *
 * Returns the SAME shape as the bot's `/api/auth/config` endpoint
 * so existing callers (`fetchAuthConfig`, OIDC redirect helpers,
 * etc.) keep working unchanged when this replaces the proxy.
 */
export async function getAuthConfig(
  client: Redis = getRedisClient(),
): Promise<AuthConfig> {
  const keysArr = [
    KEYS.mode,
    KEYS.basic_user,
    KEYS.basic_hash,
    KEYS.session_secret,
    KEYS.oidc_issuer,
    KEYS.oidc_client_id,
    KEYS.oidc_client_secret,
    KEYS.oidc_scopes,
  ];
  const vals = await client.mget(...keysArr);

  const envMode = process.env.AUTH_MODE || "";
  const envIssuer = process.env.OIDC_ISSUER || "";
  const envClientId = process.env.OIDC_CLIENT_ID || "";
  const envClientSecret = process.env.OIDC_CLIENT_SECRET || "";
  const envScopes = process.env.OIDC_SCOPES || "";

  const mode = envMode || vals[0] || "disabled";
  return {
    mode: isValidMode(mode) ? mode : "disabled",
    basic_user: vals[1] || "",
    basic_hash: vals[2] || "",
    session_secret: vals[3] || "",
    oidc_issuer: envIssuer || vals[4] || "",
    oidc_client_id: envClientId || vals[5] || "",
    oidc_client_secret: envClientSecret || vals[6] || "",
    oidc_scopes: envScopes || vals[7] || DEFAULT_OIDC_SCOPES,
  };
}

/**
 * Partial update of auth-config. Same semantics as the bot's
 * POST /api/auth/configure: only keys present in `updates` are
 * touched; empty-string value = delete the key (so the env-first
 * fallback or default kicks in).
 *
 * Does NOT touch session_secret here — that has its own
 * atomic-init helper (`ensureSessionSecret`) below to avoid
 * accidentally rotating it.
 */
export async function setAuthConfig(
  updates: Partial<Omit<AuthConfig, "session_secret">>,
  client: Redis = getRedisClient(),
): Promise<void> {
  const mapping: Array<[keyof typeof updates, string]> = [
    ["mode", KEYS.mode],
    ["basic_user", KEYS.basic_user],
    ["basic_hash", KEYS.basic_hash],
    ["oidc_issuer", KEYS.oidc_issuer],
    ["oidc_client_id", KEYS.oidc_client_id],
    ["oidc_client_secret", KEYS.oidc_client_secret],
    ["oidc_scopes", KEYS.oidc_scopes],
  ];
  const pipe = client.pipeline();
  for (const [arg, key] of mapping) {
    if (arg in updates) {
      const val = updates[arg];
      if (val === undefined || val === null || val === "") {
        pipe.del(key);
      } else {
        pipe.set(key, val);
      }
    }
  }
  await pipe.exec();
}

/**
 * Generate session_secret if missing. Returns the current secret.
 * Mirrors `control.ensure_session_secret`: SET NX so two
 * concurrent first-init callers don't clobber each other.
 *
 * 48 random bytes (URL-safe base64, ~64 chars). Matches the bot's
 * `secrets.token_urlsafe(48)` to keep cross-process generated
 * tokens compatible.
 */
export async function ensureSessionSecret(
  client: Redis = getRedisClient(),
): Promise<string> {
  const existing = await client.get(KEYS.session_secret);
  if (existing) return existing;
  const candidate = randomBytes(48).toString("base64url");
  // SET NX — atomic set-if-not-exists. The first caller writes
  // the candidate; the second one's set is a no-op. Either way
  // we re-read to get the canonical winner.
  await client.set(KEYS.session_secret, candidate, "NX");
  const winner = await client.get(KEYS.session_secret);
  return winner || candidate;
}
