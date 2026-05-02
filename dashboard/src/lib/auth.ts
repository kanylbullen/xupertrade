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

export const SESSION_COOKIE = "hypertrade_session";
const SESSION_TTL_SECONDS = 60 * 60 * 24 * 7; // 7 days

export type AuthMode = "disabled" | "basic" | "oidc";

export type AuthConfig = {
  mode: AuthMode;
  basic_user_set: boolean;
  oidc_issuer: string;
  oidc_client_id: string;
  oidc_scopes: string;
  session_secret: string;
};

export type SessionPayload = {
  sub: string;
  iat: number;
  exp: number;
};

function botUrlInternal(): string {
  // For server-side calls inside the dashboard container, talk to the
  // testnet bot — it's the canonical owner of auth config (only one bot
  // instance needs to hold the auth state, all dashboard sessions look
  // the same regardless of which mode the user is viewing).
  return process.env.BOT_API_URL_TESTNET || "http://bot-testnet:8001";
}

/** Fetch (with short timeout) the auth config from the bot.
 *  Falls back to {mode: "disabled"} if the bot is unreachable — degrades
 *  to no-auth rather than locking the user out.
 *  Cached in-process for 30s so the proxy doesn't hammer the bot. */
let _cached: { at: number; cfg: AuthConfig } | null = null;
const CACHE_TTL_MS = 30_000;

export async function fetchAuthConfig(force = false): Promise<AuthConfig> {
  const now = Date.now();
  if (!force && _cached && now - _cached.at < CACHE_TTL_MS) {
    return _cached.cfg;
  }
  try {
    const res = await fetch(`${botUrlInternal()}/api/auth/config`, {
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) {
      const cfg = defaultConfig();
      _cached = { at: now, cfg };
      return cfg;
    }
    const cfg = (await res.json()) as AuthConfig;
    _cached = { at: now, cfg };
    return cfg;
  } catch {
    const cfg = defaultConfig();
    _cached = { at: now, cfg };
    return cfg;
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
    session_secret: "",
  };
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
