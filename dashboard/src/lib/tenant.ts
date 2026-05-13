/**
 * Tenant resolver — multi-tenancy Phase 2c.
 *
 * Maps an authenticated dashboard session (Authentik OIDC `sub` claim
 * via `lib/auth.ts`) to a row in the `tenants` table. Auto-creates
 * a tenant on first sight so users don't need a separate "register"
 * flow — the Authentik group membership IS the registration.
 */

import { createHash, randomUUID } from "node:crypto";

import { eq } from "drizzle-orm";

import { db, tenants } from "./db";
import {
  SESSION_COOKIE,
  type SessionPayload,
  getSessionSecret,
  verifySession,
} from "./auth";
import { loadKey } from "./crypto/k-cache";
import { isSessionRevoked } from "./session-store";

export type Tenant = typeof tenants.$inferSelect;

/**
 * Sentinel returned by `getCurrentTenant` when the session is valid
 * AND the tenant row exists but `is_active=false` (operator-disabled
 * offboarding). Distinct from `null` (no/invalid session) so callers
 * can 403 with a useful message instead of bouncing the user back to
 * /login forever via the standard 401.
 *
 * Security audit M-2: prior to this, `is_active` was a dead column —
 * disabled tenants continued to operate normally because no resolver
 * checked the flag.
 */
export const TENANT_DISABLED = "disabled" as const;
export type TenantDisabled = typeof TENANT_DISABLED;

/**
 * Stable session-id derived from the signed cookie value: sha256
 * truncated to 32 hex chars. Same cookie → same id (deterministic);
 * different cookies → different ids (isolation). The cookie itself is
 * MAC-verified by `verifySession` upstream, so we never key off
 * forgeable input.
 */
export function getSessionIdFromRequest(req: Request): string | null {
  const cookieHeader = req.headers.get("cookie");
  if (!cookieHeader) return null;
  const match = cookieHeader.match(
    new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`),
  );
  if (!match) return null;
  return createHash("sha256").update(match[1]).digest("hex").slice(0, 32);
}

/**
 * Read the session cookie from a `Request` and verify it. Returns
 * the payload (`{sub, iat, exp}`) or `null` for unauthenticated.
 */
export async function getSessionFromRequest(
  req: Request,
): Promise<SessionPayload | null> {
  const cookieHeader = req.headers.get("cookie");
  if (!cookieHeader) return null;
  const match = cookieHeader.match(
    new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`),
  );
  if (!match) return null;
  const secret = await getSessionSecret().catch(() => "");
  if (!secret) return null;
  const payload = verifySession(match[1], secret);
  if (payload === null) return null;
  // H-3: revoked sessions look identical to "no session" upstream so
  // requireTenant returns 401 cleanly. Fail-closed on Redis error.
  if (await isSessionRevoked(match[1])) return null;
  return payload;
}

/**
 * Look up the tenant for the authenticated session, creating one if
 * this Authentik sub hasn't been seen before. Returns null when
 * no/invalid session, or `TENANT_DISABLED` when the row exists but
 * `is_active=false` (security audit M-2 — operator-driven offboarding).
 *
 * Note: we deliberately fetch the row WITHOUT filtering on `is_active`
 * and check the flag in JS, rather than adding `eq(isActive, true)` to
 * the WHERE. A filtered query would make a disabled row look identical
 * to "no row", which would silently fall through to the autoCreate
 * path below — re-creating the tenant the operator just disabled.
 */
export async function getCurrentTenant(
  req: Request,
): Promise<Tenant | null | TenantDisabled> {
  const session = await getSessionFromRequest(req);
  if (session === null) return null;

  const existing = await db
    .select()
    .from(tenants)
    .where(eq(tenants.authentikSub, session.sub))
    .limit(1);
  if (existing.length > 0) {
    if (existing[0].isActive !== true) return TENANT_DISABLED;
    return existing[0];
  }

  // First time we see this sub — create a tenant. Email defaults to
  // the sub itself (Authentik's sub is typically email-shaped already);
  // a richer profile sync can update it later.
  //
  // Concurrency: two parallel requests for a brand-new sub can both
  // miss the SELECT, then collide on the unique index when both try
  // INSERT. `onConflictDoNothing` makes the second one a silent no-op;
  // the re-SELECT below picks up whichever row won the race.
  await db
    .insert(tenants)
    .values({
      id: randomUUID(),
      authentikSub: session.sub,
      email: session.sub,
      displayName: session.sub,
    })
    .onConflictDoNothing({ target: tenants.authentikSub });

  const created = await db
    .select()
    .from(tenants)
    .where(eq(tenants.authentikSub, session.sub))
    .limit(1);
  if (created.length === 0) {
    // Should be impossible — we just inserted (or another request did).
    // If we get here, something is very wrong with the DB.
    throw new Error("tenant insert succeeded but row not found");
  }
  // M-2 belt-and-braces: a pre-existing disabled row could have won
  // the onConflictDoNothing race. Treat it as disabled, not as a
  // freshly-created active tenant.
  if (created[0].isActive !== true) return TENANT_DISABLED;
  return created[0];
}

/**
 * Look up the tenant row for the calling session WITHOUT enforcing
 * `is_active`. Internal helper for `requireOperator` only — operators
 * are special: an operator who flips their own `is_active=false` (or
 * has it flipped by another operator during a botched offboarding)
 * must still be able to sign in and re-enable themselves, otherwise
 * the platform can be locked out of operator access entirely.
 *
 * Returns null when there's no/invalid session or the row doesn't
 * exist yet. Does NOT auto-create — operators must already exist via
 * Phase 6b backfill.
 */
export async function getTenantRowBypassActive(
  req: Request,
): Promise<Tenant | null> {
  const session = await getSessionFromRequest(req);
  if (session === null) return null;
  const rows = await db
    .select()
    .from(tenants)
    .where(eq(tenants.authentikSub, session.sub))
    .limit(1);
  return rows[0] ?? null;
}

/**
 * Return tenant or throw a Response — convenience for API routes.
 *  - 401 when there's no/invalid session
 *  - 403 (`{error: "tenant-disabled"}`) when the tenant row exists but
 *    `is_active=false` (M-2). Distinct from 401 so the dashboard can
 *    show a clear "your account has been disabled" message instead of
 *    bouncing back to /login forever.
 */
export async function requireTenant(req: Request): Promise<Tenant> {
  const t = await getCurrentTenant(req);
  if (t === null) {
    throw new Response(JSON.stringify({ error: "not authenticated" }), {
      status: 401,
      headers: { "content-type": "application/json" },
    });
  }
  if (t === TENANT_DISABLED) {
    throw new Response(JSON.stringify({ error: "tenant-disabled" }), {
      status: 403,
      headers: { "content-type": "application/json" },
    });
  }
  return t;
}

/**
 * Fetch the tenant's cached K from Redis, or throw a 401 telling the
 * caller to POST /api/tenant/me/unlock first. Used by secret-CRUD
 * endpoints (Phase 2d) and bot-start endpoints (Phase 3) — anything
 * that needs to decrypt or re-encrypt a stored secret.
 *
 * Returns the 32-byte K. Caller is responsible for not logging it.
 */
export async function requireUnlockedKey(
  req: Request,
  tenant: Tenant,
): Promise<Buffer> {
  const sessionId = getSessionIdFromRequest(req);
  if (sessionId === null) {
    throw new Response(JSON.stringify({ error: "no session" }), {
      status: 401,
      headers: { "content-type": "application/json" },
    });
  }
  const k = await loadKey(tenant.id, sessionId);
  if (k === null) {
    throw new Response(
      JSON.stringify({ error: "tenant locked; POST /api/tenant/me/unlock first" }),
      { status: 401, headers: { "content-type": "application/json" } },
    );
  }
  return k;
}
