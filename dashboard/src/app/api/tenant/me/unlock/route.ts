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
import { checkRateLimit } from "@/lib/rate-limit";
import {
  getSessionIdFromRequest,
  requireTenant,
} from "@/lib/tenant";

export const dynamic = "force-dynamic";

// H-2: 10 passphrase attempts per 15 min per (tenant_id, ip). Argon2id
// at our params runs ~100ms — without a gate that's ~10 guesses/sec
// indefinitely from a single IP. With this gate an attacker is held
// to ~1k guesses/day per IP per tenant, which gives operators time to
// notice via the audit log (every failed attempt writes a row).
const UNLOCK_RATE_LIMIT_MAX = 10;
const UNLOCK_RATE_LIMIT_WINDOW_SEC = 900;

function getClientIp(req: Request): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const first = xff.split(",")[0]?.trim();
    if (first) return first;
  }
  return "unknown";
}

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

  // H-2: rate-limit BEFORE Argon2id derivation. Without this an
  // attacker with a valid session cookie can pin 100% of one CPU
  // core forever guessing passphrases. We key on (tenant_id, ip) so
  // a single attacker's misbehavior doesn't lock the legit tenant
  // out from a different network.
  const ip = getClientIp(req);
  const rl = await checkRateLimit(
    "tenant-unlock",
    `${tenant.id}:${ip}`,
    UNLOCK_RATE_LIMIT_MAX,
    UNLOCK_RATE_LIMIT_WINDOW_SEC,
  );
  if (!rl.allowed) {
    await appendAuditLog(tenant.id, "tenant", "passphrase.unlock-rate-limited", {
      ip,
      reset_in_seconds: rl.resetInSeconds,
    });
    return Response.json(
      {
        error: "rate-limited",
        retry_after_seconds: rl.resetInSeconds,
      },
      {
        status: 429,
        headers: { "Retry-After": String(rl.resetInSeconds) },
      },
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
    // H-2 / M-4: every failed attempt writes an audit row so the
    // operator can spot a brute-force pattern (10/15min/IP gate
    // above slows it; this gate makes it visible).
    await appendAuditLog(tenant.id, "tenant", "passphrase.unlock-failed", {
      ip,
    });
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
