/**
 * OIDC (OpenID Connect) authorization-code flow with PKCE + state.
 *
 * Config (issuer, client_id, client_secret, scopes) is read
 * directly from Redis via lib/auth-config.ts — env-first override,
 * then dashboard:auth:oidc:* keys. The client_secret never leaves
 * the dashboard process; the public /api/auth/config endpoint
 * strips it from its response.
 *
 * Flow:
 *   /login (mode=oidc) → click → /api/auth/oidc/start
 *     → discover provider, generate PKCE+state, set state cookie,
 *        redirect to provider's authorize endpoint
 *   provider → /api/auth/oidc/callback?code=...&state=...
 *     → verify state, exchange code, validate id_token,
 *        set session cookie, redirect to /
 */

import * as client from "openid-client";
import { fetchAuthConfig, type AuthConfig } from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";

export const STATE_COOKIE = "hypertrade_oidc_state";
const STATE_TTL_SECONDS = 600; // 10 min — enough for the round trip

export const STATE_COOKIE_OPTIONS = {
  name: STATE_COOKIE,
  httpOnly: true,
  sameSite: "lax" as const,
  path: "/",
  maxAge: STATE_TTL_SECONDS,
};

export type OidcStateBundle = {
  code_verifier: string;
  state: string;
  next: string;
};

/** Encode the per-request state (verifier + state + return target) so the
 *  callback can recover what start initiated. JSON, not signed — the state
 *  param itself is the integrity check. */
export function encodeStateBundle(b: OidcStateBundle): string {
  return Buffer.from(JSON.stringify(b), "utf-8").toString("base64url");
}

export function decodeStateBundle(s: string): OidcStateBundle | null {
  try {
    const parsed = JSON.parse(Buffer.from(s, "base64url").toString("utf-8"));
    if (
      typeof parsed?.code_verifier === "string" &&
      typeof parsed?.state === "string" &&
      typeof parsed?.next === "string"
    ) {
      return parsed as OidcStateBundle;
    }
  } catch {
    // fall through
  }
  return null;
}

/** Why `getOidcConfig` refused. Each value is also the `/login?error=`
 *  code the routes redirect with, so they pass it through unchanged. */
export type OidcConfigError = "auth-locked" | "oidc-misconfigured";

export type OidcConfigResult =
  | { ok: true; config: client.Configuration; cfg: AuthConfig }
  | { ok: false; error: OidcConfigError };

const MISCONFIGURED: OidcConfigResult = { ok: false, error: "oidc-misconfigured" };

/** Build the openid-client Configuration from the auth config in Redis.
 *
 *  Refuses with `auth-locked` when the resolved mode is `"locked"`, and
 *  with `oidc-misconfigured` when OIDC isn't usable (Redis unreachable,
 *  issuer/client_id/secret missing, unparsable issuer). */
export async function getOidcConfig(): Promise<OidcConfigResult> {
  const cfg = await fetchAuthConfig(true);
  if (!cfg) return MISCONFIGURED;
  // SECURITY: `resolveMode` answers "locked" when the stored auth mode
  // is gone on an install with tenants, and nothing may mint a session
  // then — `api/auth/login` refuses basic sign-in by name for the same
  // reason. A session minted while locked is useless until the lock
  // lifts, and then it is valid. The check sits here, on the same
  // forced read the rest of this function uses, and before discovery,
  // so a locked dashboard never contacts the IdP. It precedes the
  // issuer check so the answer is `auth-locked` whatever OIDC fields
  // happen to survive.
  if (cfg.mode === "locked") return { ok: false, error: "auth-locked" };
  if (!cfg.oidc_issuer || !cfg.oidc_client_id) return MISCONFIGURED;

  // The public /api/auth/config endpoint deliberately strips
  // client_secret (and session_secret too). Here we read it from
  // Redis directly via lib/auth-config — server-side only, never
  // leaves the dashboard process.
  const secret = await fetchOidcSecret();
  if (!secret) return MISCONFIGURED;

  let issuer: URL;
  try {
    issuer = new URL(cfg.oidc_issuer);
  } catch {
    return MISCONFIGURED;
  }

  const config = await client.discovery(
    issuer,
    cfg.oidc_client_id,
    secret,
  );

  return { ok: true, config, cfg };
}

/** Internal — fetch the OIDC client secret. PR 4b: now reads
 *  Redis directly via lib/auth-config.ts (env-first override
 *  → Redis → empty default). Replaces the previous bot-proxied
 *  /api/auth/oidc-secret call. Same data, one fewer hop.
 */
async function fetchOidcSecret(): Promise<string | null> {
  try {
    const cfg = await getAuthConfig();
    return cfg.oidc_client_secret || null;
  } catch {
    return null;
  }
}

/** Resolve the OIDC redirect_uri.
 *
 *  Order of precedence:
 *  1. PUBLIC_URL env var (recommended in production — survives container
 *     name changes and proxy hops)
 *  2. DASHBOARD_URL env var (already used for CORS — sensible second pick)
 *  3. The incoming request's origin (works for local dev; breaks in
 *     containers where the bound hostname is a docker-id)
 *
 *  Always points at /api/auth/oidc/callback. The provider must accept
 *  this exact string as a registered redirect URI.
 */
export function resolveRedirectUri(req: Request): string {
  const explicit =
    (process.env.PUBLIC_URL || process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, "");
  if (explicit) {
    return `${explicit}/api/auth/oidc/callback`;
  }
  const url = new URL(req.url);
  return new URL("/api/auth/oidc/callback", url).toString();
}

/** Re-exported so existing server-side importers (`oidc/start`,
 *  `oidc/callback`) keep their import path. The implementation lives
 *  in `lib/safe-next.ts` because the login form is a Client Component
 *  and cannot import this server-only module — it used to carry its
 *  own copy of the rules, and therefore its own copy of the bug. */
export { safeNext } from "./safe-next";
