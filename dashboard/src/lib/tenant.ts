/**
 * Tenant resolver — multi-tenancy Phase 2c.
 *
 * Maps an authenticated dashboard session (Authentik OIDC `sub` claim
 * via `lib/auth.ts`) to a row in the `tenants` table. Auto-creates
 * a tenant on first sight so users don't need a separate "register"
 * flow — the Authentik group membership IS the registration.
 */

import { randomUUID } from "node:crypto";

import { eq } from "drizzle-orm";

import { db, tenants } from "./db";
import {
  SESSION_COOKIE,
  type SessionPayload,
  getSessionSecret,
  verifySession,
} from "./auth";

export type Tenant = typeof tenants.$inferSelect;

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
  return verifySession(match[1], secret);
}

/**
 * Look up the tenant for the authenticated session, creating one if
 * this Authentik sub hasn't been seen before. Returns null when
 * no/invalid session.
 */
export async function getCurrentTenant(req: Request): Promise<Tenant | null> {
  const session = await getSessionFromRequest(req);
  if (session === null) return null;

  const existing = await db
    .select()
    .from(tenants)
    .where(eq(tenants.authentikSub, session.sub))
    .limit(1);
  if (existing.length > 0) return existing[0];

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
  return created[0];
}

/** Return tenant or throw a Response (401) — convenience for API routes. */
export async function requireTenant(req: Request): Promise<Tenant> {
  const t = await getCurrentTenant(req);
  if (t === null) {
    throw new Response(JSON.stringify({ error: "not authenticated" }), {
      status: 401,
      headers: { "content-type": "application/json" },
    });
  }
  return t;
}
