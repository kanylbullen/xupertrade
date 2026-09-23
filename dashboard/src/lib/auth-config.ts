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

export type AuthMode = "disabled" | "basic" | "oidc" | "locked";

/**
 * The three modes an operator can actually *set*. `"locked"` is
 * resolved-only: `resolveMode` returns it when the stored mode is
 * missing or unreadable and we refuse to guess. It is never written
 * to Redis and never accepted from env or the configure endpoint.
 */
export type ConfigurableAuthMode = Exclude<AuthMode, "locked">;

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

/** Is `v` one of the modes an operator can set? Exported so
 *  `phase-sync.ts` validates `AUTH_MODE` against the same list the
 *  resolver does, instead of keeping a second copy. */
export function isConfigurableAuthMode(
  v: string | null | undefined,
): v is ConfigurableAuthMode {
  return v === "disabled" || v === "basic" || v === "oidc";
}

/**
 * Does this installation already have tenants? Used only by
 * `resolveMode`'s missing-key branch, to tell a genuinely fresh
 * install (bootstrap must stay possible) from an existing one whose
 * Redis was flushed (must fail closed).
 *
 * Lazily imported so the Postgres client is only pulled in on the
 * rare path that needs it — the steady state has a stored mode and
 * never reaches here.
 *
 * On any DB error we answer `true` ("tenants exist"), which pushes
 * `resolveMode` towards `"locked"`. Fail closed: a Postgres outage
 * must not look like a fresh install and re-open the dashboard.
 */
async function defaultHasAnyTenant(): Promise<boolean> {
  try {
    const { db, tenants } = await import("./db");
    const rows = await db.select({ id: tenants.id }).from(tenants).limit(1);
    return rows.length > 0;
  } catch (err) {
    // Log the error TYPE only — a postgres connection error can carry
    // the DSN, and DATABASE_URL contains the password (§ 0).
    console.warn(
      "[auth-config] tenant probe failed, assuming a provisioned install",
      err instanceof Error ? err.name : typeof err,
    );
    return true;
  }
}

export type TenantProbe = () => Promise<boolean>;

/**
 * Decide the effective auth mode.
 *
 * SECURITY (analysis-2026-09-15 § 5, High). This used to be
 * `envMode || storedMode || "disabled"`, so an ABSENT
 * `dashboard:auth:mode` key resolved to `"disabled"` — and `proxy.ts`
 * lets every request through in that mode while `tenant-server.ts`
 * resolves the operator tenant. A `FLUSHALL`, a `docker compose down
 * -v`, or (until this PR) simply recreating the volume-less redis
 * container therefore served the operator's trades, P&L and positions
 * to anonymous visitors on every server-rendered page. The key going
 * missing is exactly the case where we know the least, so it is the
 * case where we must assume the most.
 *
 * The decision, in order:
 *
 *  1. `AUTH_MODE` in env — honoured, including `disabled`. This is the
 *     operator saying so out of band, in Phase, where a Redis flush
 *     cannot reach it. An unrecognised value is a typo, not a
 *     permission: `locked`.
 *  2. A stored `dashboard:auth:mode` — honoured, including
 *     `disabled`. A value that is *present* cannot be the flush we
 *     are defending against, so an operator who picked "Off" on the
 *     Options page keeps it. Garbage again means `locked`.
 *  3. Key absent — never `disabled` by default. Fall to the strictest
 *     mode the surviving config can actually serve:
 *       - a basic user + hash survive → `basic`
 *       - an OIDC issuer + client id survive (these come back from
 *         Phase env via `phase-sync.ts` on every boot) → `oidc`
 *       - nothing survives and the DB has no tenants → `disabled`,
 *         because a first boot has nothing to leak and has to be able
 *         to bootstrap
 *       - nothing survives and tenants exist → `locked`: the login
 *         page explains what happened and no page renders data. The
 *         operator recovers by setting `AUTH_MODE` (and the OIDC
 *         vars) in Phase and restarting, or by restoring `dump.rdb`.
 */
export async function resolveMode(args: {
  envMode: string;
  storedMode: string | null;
  basicConfigured: boolean;
  oidcConfigured: boolean;
  hasAnyTenant?: TenantProbe;
}): Promise<AuthMode> {
  if (args.envMode) {
    return isConfigurableAuthMode(args.envMode) ? args.envMode : "locked";
  }
  if (args.storedMode) {
    return isConfigurableAuthMode(args.storedMode) ? args.storedMode : "locked";
  }
  if (args.basicConfigured) return "basic";
  if (args.oidcConfigured) return "oidc";
  const probe = args.hasAnyTenant ?? defaultHasAnyTenant;
  if (!(await probe())) return "disabled";
  return "locked";
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
  hasAnyTenant?: TenantProbe,
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

  // Trim env values to match `lib/phase-sync.ts` which trims before
  // writing to Redis. Without this, an env with leading/trailing
  // whitespace would land in Redis trimmed but env-first reads would
  // see the raw value, recreating the cross-path drift the sync was
  // built to eliminate (Copilot review fix on PR #104).
  const envMode = (process.env.AUTH_MODE || "").trim();
  const envIssuer = (process.env.OIDC_ISSUER || "").trim();
  const envClientId = (process.env.OIDC_CLIENT_ID || "").trim();
  const envClientSecret = (process.env.OIDC_CLIENT_SECRET || "").trim();
  const envScopes = (process.env.OIDC_SCOPES || "").trim();

  const basic_user = vals[1] || "";
  const basic_hash = vals[2] || "";
  const oidc_issuer = envIssuer || vals[4] || "";
  const oidc_client_id = envClientId || vals[5] || "";

  const mode = await resolveMode({
    envMode,
    storedMode: vals[0],
    // "Configured" means usable for an actual sign-in, not merely
    // non-empty: basic needs both the username and the bcrypt hash,
    // OIDC needs both the issuer and the client id.
    basicConfigured: Boolean(basic_user && basic_hash),
    oidcConfigured: Boolean(oidc_issuer && oidc_client_id),
    hasAnyTenant,
  });

  return {
    mode,
    basic_user,
    basic_hash,
    session_secret: vals[3] || "",
    oidc_issuer,
    oidc_client_id,
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
 *
 * `mode` is narrowed to `ConfigurableAuthMode`: `"locked"` is a
 * resolved state, never a stored one. Writing it would make the
 * lockout survive a correctly-configured restart.
 */
export async function setAuthConfig(
  updates: Partial<Omit<AuthConfig, "session_secret" | "mode">> & {
    mode?: ConfigurableAuthMode;
  },
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
