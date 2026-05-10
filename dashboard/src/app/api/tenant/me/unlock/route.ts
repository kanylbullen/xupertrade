/**
 * /api/tenant/me/unlock — verify the tenant's passphrase and cache K
 * for the rest of the session, OR clear the cached K (lock).
 *
 * Multi-tenancy Phase 2c.
 *
 *   POST   { passphrase: string }   — verify + cache K
 *   DELETE                          — clear cached K (logout / lock)
 */

import { createHash } from "node:crypto";

import { eq } from "drizzle-orm";

import {
  cacheKey,
  clearKey,
} from "@/lib/crypto/k-cache";
import {
  deriveKey,
  verify,
} from "@/lib/crypto/passphrase";
import { db, tenants } from "@/lib/db";
import { SESSION_COOKIE } from "@/lib/auth";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

/** Derive a stable session-id from the cookie itself (sha256 of the
 * signed cookie value). Used as the key suffix in Redis so the
 * cookie's signature doesn't have to be persisted server-side.
 * sha256 is one-way + deterministic; same cookie value always maps
 * to the same Redis key, different cookies map to different keys. */
function sessionIdFromCookie(req: Request): string | null {
  const cookieHeader = req.headers.get("cookie");
  if (!cookieHeader) return null;
  const match = cookieHeader.match(
    new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`),
  );
  if (!match) return null;
  return createHash("sha256").update(match[1]).digest("hex").slice(0, 32);
}

export async function POST(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (resp) {
    return resp as Response;
  }

  if (tenant.passphraseSalt === null || tenant.passphraseVerifier === null) {
    return Response.json(
      { error: "passphrase not set; POST /api/tenant/me/passphrase first" },
      { status: 409 },
    );
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: "body must be valid JSON" }, { status: 400 });
  }
  const passphrase = (body as { passphrase?: unknown })?.passphrase;
  if (typeof passphrase !== "string") {
    return Response.json(
      { error: "field 'passphrase' is required (string)" },
      { status: 400 },
    );
  }

  const k = await deriveKey(passphrase, tenant.passphraseSalt);
  if (!verify(k, tenant.passphraseVerifier)) {
    return Response.json({ error: "wrong passphrase" }, { status: 401 });
  }

  const sessionId = sessionIdFromCookie(req);
  if (sessionId === null) {
    // Should never happen — requireTenant already verified the
    // session — but defensive.
    return Response.json({ error: "no session" }, { status: 401 });
  }

  await cacheKey(tenant.id, sessionId, k);
  await db
    .update(tenants)
    .set({ lastLoginAt: new Date() })
    .where(eq(tenants.id, tenant.id));

  return Response.json({ unlocked: true });
}

export async function DELETE(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (resp) {
    return resp as Response;
  }

  const sessionId = sessionIdFromCookie(req);
  if (sessionId !== null) {
    await clearKey(tenant.id, sessionId);
  }
  return Response.json({ locked: true });
}
