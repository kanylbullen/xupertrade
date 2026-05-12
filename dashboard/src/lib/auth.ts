/**
 * Dashboard auth — HMAC-signed session cookies, no external deps.
 *
 * The auth config (mode, OIDC creds, basic credentials hash) is owned by
 * the bot and exposed via /api/auth/config. The dashboard is stateless —
 * it just signs/verifies cookies and proxies credential checks to the bot.
 *
 * Cookie format:  base64url(payload).hmac_sha256(payload, secret)
 * Payload: JSON {sub: "username", iat: epoch_seconds, exp: epoch_seconds}
 */

import { createHmac, timingSafeEqual } from "crypto";

import { ensureSessionSecret, getAuthConfig } from "./auth-config";

export const SESSION_COOKIE = "hypertrade_session";
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 7; // 7 days

export type AuthMode = "disabled" | "basic" | "oidc";

export type AuthConfig = {
  mode: AuthMode;
  basic_user_set: boolean;
  oidc_issuer: string;
  oidc_client_id: string;
  oidc_scopes: string;
};

export type SessionPayload = {
  sub: string;
  iat: number;
  exp: number;
};

/** Fetch the auth config — now reads Redis directly via
 *  `auth-config.ts:getAuthConfig` (PR 4a) instead of proxying
 *  through the bot. Same in-process cache + same fail-closed
 *  semantics (returns null on error so proxy.ts denies).
 */
let _cached: { at: number; cfg: AuthConfig } | null = null;
const CACHE_TTL_MS = 30_000;

export async function fetchAuthConfig(force = false): Promise<AuthConfig | null> {
  const now = Date.now();
  if (!force && _cached && now - _cached.at < CACHE_TTL_MS) {
    return _cached.cfg;
  }
  try {
    const raw = await getAuthConfig();
    const cfg: AuthConfig = {
      mode: raw.mode,
      basic_user_set: Boolean(raw.basic_user),
      oidc_issuer: raw.oidc_issuer,
      oidc_client_id: raw.oidc_client_id,
      oidc_scopes: raw.oidc_scopes,
    };
    _cached = { at: now, cfg };
    return cfg;
  } catch {
    // SECURITY: do NOT cache a default-disabled config on failure.
    // proxy.ts uses null as "fail closed" — caching disabled here
    // would let an attacker who induces a transient Redis outage
    // walk past auth for the entire 30s cache TTL.
    return null;
  }
}

export function invalidateAuthCache(): void {
  _cached = null;
}

function defaultConfig(): AuthConfig {
  return {
    mode: "disabled",
    basic_user_set: false,
    oidc_issuer: "",
    oidc_client_id: "",
    oidc_scopes: "openid profile email",
  };
}

/** Fetch the dashboard's session-cookie HMAC secret.
 *
 *  PR 4a: now reads/initializes the secret directly via Redis
 *  (`auth-config.ts:ensureSessionSecret`) instead of proxying
 *  through a bot endpoint. ensureSessionSecret is atomic
 *  (SET NX) so two dashboard processes that race on first init
 *  end up with the same value.
 *
 *  Returns "" only if Redis is unreachable, in which case
 *  signSession/verifySession refuse to operate (fail-closed).
 *  Cached 60s — secret rarely changes.
 */
let _secretCached: { at: number; value: string } | null = null;
const SECRET_CACHE_TTL_MS = 60_000;

export async function getSessionSecret(force = false): Promise<string> {
  const now = Date.now();
  if (!force && _secretCached && now - _secretCached.at < SECRET_CACHE_TTL_MS) {
    return _secretCached.value;
  }
  try {
    const value = await ensureSessionSecret();
    _secretCached = { at: now, value };
    return value;
  } catch {
    _secretCached = { at: now, value: "" };
    return "";
  }
}

export function invalidateSessionSecretCache(): void {
  _secretCached = null;
}

function b64urlEncode(buf: Buffer): string {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(s: string): Buffer {
  const pad = (4 - (s.length % 4)) % 4;
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat(pad);
  return Buffer.from(b64, "base64");
}

export function signSession(payload: SessionPayload, secret: string): string {
  const json = Buffer.from(JSON.stringify(payload), "utf-8");
  const body = b64urlEncode(json);
  const mac = createHmac("sha256", secret).update(body).digest();
  return `${body}.${b64urlEncode(mac)}`;
}

export function verifySession(
  cookie: string,
  secret: string,
): SessionPayload | null {
  if (!cookie || !secret) return null;
  const [body, sig] = cookie.split(".");
  if (!body || !sig) return null;
  const expected = createHmac("sha256", secret).update(body).digest();
  let provided: Buffer;
  try {
    provided = b64urlDecode(sig);
  } catch {
    return null;
  }
  if (provided.length !== expected.length) return null;
  if (!timingSafeEqual(provided, expected)) return null;

  let payload: SessionPayload;
  try {
    payload = JSON.parse(b64urlDecode(body).toString("utf-8")) as SessionPayload;
  } catch {
    return null;
  }
  if (typeof payload.exp !== "number" || payload.exp < Math.floor(Date.now() / 1000)) {
    return null;
  }
  return payload;
}

export function newSessionPayload(username: string): SessionPayload {
  const now = Math.floor(Date.now() / 1000);
  return { sub: username, iat: now, exp: now + SESSION_TTL_SECONDS };
}

export const COOKIE_OPTIONS = {
  name: SESSION_COOKIE,
  httpOnly: true,
  sameSite: "lax" as const,
  path: "/",
  maxAge: SESSION_TTL_SECONDS,
  // secure flag is auto-applied when DASHBOARD_URL starts with https:
  // (Next.js sets it automatically when behind https in prod)
};
