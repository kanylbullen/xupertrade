/**
 * /api/tenant/me/unlock — verify the tenant's passphrase and cache K
 * for the rest of the session, OR clear the cached K (lock).
 *
 * Multi-tenancy Phase 2c.
 *
 *   POST   { passphrase: string }   — verify + cache K
 *   DELETE                          — clear cached K (logout / lock)
 */

import { eq } from "drizzle-orm";

import { appendAuditLog } from "@/lib/audit-log";
import {
  cacheKey,
  clearKey,
} from "@/lib/crypto/k-cache";
import {
  deriveKey,
  verify,
} from "@/lib/crypto/passphrase";
import { db, tenants } from "@/lib/db";
import {
  getSessionIdFromRequest,
  requireTenant,
} from "@/lib/tenant";

export const dynamic = "force-dynamic";

export async function POST(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    // requireTenant throws Response for the auth-fail case (401). Only
    // intercept those; let real errors (DB/Redis/network) propagate so
    // Next reports them properly instead of returning an Error-as-Response.
    if (err instanceof Response) return err;
    throw err;
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

  const sessionId = getSessionIdFromRequest(req);
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
  await appendAuditLog(tenant.id, "tenant", "passphrase.unlock");

  return Response.json({ unlocked: true });
}

export async function DELETE(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    // requireTenant throws Response for the auth-fail case (401). Only
    // intercept those; let real errors (DB/Redis/network) propagate so
    // Next reports them properly instead of returning an Error-as-Response.
    if (err instanceof Response) return err;
    throw err;
  }

  const sessionId = getSessionIdFromRequest(req);
  if (sessionId !== null) {
    await clearKey(tenant.id, sessionId);
  }
  await appendAuditLog(tenant.id, "tenant", "passphrase.lock");
  return Response.json({ locked: true });
}
