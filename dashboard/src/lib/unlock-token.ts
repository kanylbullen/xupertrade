/**
 * Signed unlock-link tokens (PR 3c).
 *
 * When a tenant's bot is locked and we want to DM them an
 * unlock deeplink, we encode `{tenant_id, exp}` into a short-lived
 * token and sign it with the dashboard's `SESSION_SECRET`. The
 * /unlock page validates the signature + expiry before showing
 * the passphrase form.
 *
 * Format (base64url):
 *   payload.signature
 *
 * Where:
 *   payload   = base64url(JSON.stringify({sub, exp}))
 *   signature = base64url(HMAC-SHA256(SESSION_SECRET, payload))
 *
 * Same primitive shape as `auth.ts:signSession` — different domain
 * so a session cookie can't be replayed as an unlock token (and
 * vice versa).
 */

import { createHmac, timingSafeEqual } from "node:crypto";

import { getSessionSecret } from "./auth";

const UNLOCK_TOKEN_TTL_SECONDS = 600; // 10 min — enough to switch apps + read
const SIGNATURE_DOMAIN = "unlock-token-v1";

export type UnlockTokenPayload = {
  sub: string; // tenant_id (UUID)
  exp: number; // unix-seconds expiry
};

function b64url(buf: Buffer): string {
  return buf
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function b64urlDecode(s: string): Buffer {
  // Restore padding for `Buffer.from(..., "base64")`.
  const padded = s + "=".repeat((4 - (s.length % 4)) % 4);
  return Buffer.from(padded.replace(/-/g, "+").replace(/_/g, "/"), "base64");
}

function sign(payload: string, secret: string): string {
  const mac = createHmac("sha256", secret)
    .update(SIGNATURE_DOMAIN)
    .update(":")
    .update(payload)
    .digest();
  return b64url(mac);
}

/**
 * Mint a signed unlock token for `tenantId`. Caller controls the
 * TTL via `ttlSeconds` (default 10 min — short enough that a
 * leaked link expires before an attacker can use it offline).
 */
export async function mintUnlockToken(
  tenantId: string,
  ttlSeconds: number = UNLOCK_TOKEN_TTL_SECONDS,
): Promise<string> {
  const secret = await getSessionSecret();
  // Fail closed when no secret is configured (bot unreachable, no
  // basic creds, etc. — getSessionSecret returns "" in those cases).
  // Signing with "" would produce a HMAC anyone can recompute, and
  // verifyUnlockToken would later succeed for any forged token.
  if (!secret) {
    throw new Error("cannot mint unlock token: session secret not configured");
  }
  const payload: UnlockTokenPayload = {
    sub: tenantId,
    exp: Math.floor(Date.now() / 1000) + ttlSeconds,
  };
  const payloadB64 = b64url(Buffer.from(JSON.stringify(payload)));
  const sig = sign(payloadB64, secret);
  return `${payloadB64}.${sig}`;
}

/**
 * Verify a signed unlock token. Returns the payload on success
 * or `null` if the signature is invalid, the token is malformed,
 * or it has expired. Caller is responsible for any further checks
 * (e.g. that `sub` matches the currently-authenticated tenant).
 */
export async function verifyUnlockToken(
  token: string,
): Promise<UnlockTokenPayload | null> {
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [payloadB64, providedSig] = parts;
  if (!payloadB64 || !providedSig) return null;

  let secret: string;
  try {
    secret = await getSessionSecret();
  } catch {
    return null;
  }
  // Match mintUnlockToken's fail-closed behavior. Without this an
  // attacker who knows the deployment is misconfigured (or who
  // forces a config wipe) could submit any token signed with HMAC-
  // SHA256(""), since this function would happily compute the
  // expected sig with the same empty key and pass timingSafeEqual.
  if (!secret) return null;
  const expectedSig = sign(payloadB64, secret);
  const a = b64urlDecode(providedSig);
  const b = b64urlDecode(expectedSig);
  if (a.length !== b.length) return null;
  if (!timingSafeEqual(a, b)) return null;

  let payload: UnlockTokenPayload;
  try {
    payload = JSON.parse(b64urlDecode(payloadB64).toString("utf8"));
  } catch {
    return null;
  }
  if (
    typeof payload.sub !== "string" ||
    typeof payload.exp !== "number" ||
    payload.exp < Math.floor(Date.now() / 1000)
  ) {
    return null;
  }
  return payload;
}
