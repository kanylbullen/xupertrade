/**
 * OIDC (OpenID Connect) authorization-code flow with PKCE + state.
 *
 * Config (issuer, client_id, client_secret, scopes) is fetched from the
 * bot — same Redis-backed config the basic-auth path uses.
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

/** Build the openid-client Configuration from the auth config in Redis.
 *  Returns null when OIDC isn't configured (issuer/client_id missing). */
export async function getOidcConfig(): Promise<{
  config: client.Configuration;
  cfg: AuthConfig;
} | null> {
  const cfg = await fetchAuthConfig(true);
  if (!cfg) return null;
  if (!cfg.oidc_issuer || !cfg.oidc_client_id) return null;

  // The public /api/auth/config endpoint deliberately strips client_secret
  // (and session_secret too — both are fetched via API_KEY-gated bot
  // endpoints so they never appear in publicly-readable responses).
  const secret = await fetchOidcSecret();
  if (!secret) return null;

  let issuer: URL;
  try {
    issuer = new URL(cfg.oidc_issuer);
  } catch {
    return null;
  }

  const config = await client.discovery(
    issuer,
    cfg.oidc_client_id,
    secret,
  );

  return { config, cfg };
}

/** Internal — fetch the OIDC client secret from the bot. Requires API_KEY
 *  on the dashboard side (set via env var). The bot's /api/auth/secret
 *  endpoint returns it only when API_KEY matches. */
async function fetchOidcSecret(): Promise<string | null> {
  const botUrl =
    process.env.BOT_API_URL_TESTNET || "http://bot-testnet:8001";
  const apiKey = process.env.API_KEY || "";
  try {
    const res = await fetch(`${botUrl}/api/auth/oidc-secret`, {
      method: "GET",
      headers: apiKey ? { "X-Api-Key": apiKey } : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { secret?: string };
    return data.secret || null;
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

/** Reject hostile or non-sensical redirect targets after login. */
export function safeNext(raw: string): string {
  if (!raw || !raw.startsWith("/")) return "/";
  if (raw.startsWith("//")) return "/";
  if (raw === "/login" || raw.startsWith("/login?")) return "/";
  if (raw.startsWith("/api/")) return "/";
  return raw;
}
