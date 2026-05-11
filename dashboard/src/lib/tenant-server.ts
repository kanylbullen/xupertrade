/**
 * Server-component tenant resolver — multi-tenancy Phase 6c PR ζ.
 *
 * `requireTenant` in `tenant.ts` reads cookies via a `Request` header
 * because it's meant for API route handlers. Server components don't
 * receive a Request — they read cookies via `next/headers`. This
 * helper bridges the two so pages can call `requireTenantServer()`
 * and either get the tenant or redirect to /login.
 *
 * proxy.ts already gates the page routes for unauthenticated users
 * (redirects to /login before the server component runs), so in
 * practice this helper should always resolve. The redirect-on-null
 * is a defensive belt-and-braces in case proxy.ts misses a path
 * pattern — better to bounce to login than render a half-broken
 * page with operator's data.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { db, tenants } from "./db";
import {
  SESSION_COOKIE,
  getSessionSecret,
  verifySession,
} from "./auth";
import { randomUUID } from "node:crypto";
import { eq } from "drizzle-orm";
import type { Tenant } from "./tenant";

/**
 * Resolve the calling tenant from the server-side cookie store.
 * Redirects to /login when there's no/invalid session — server
 * components can't return Responses, so we use Next.js's `redirect()`.
 *
 * Auto-creates the tenant row on first sight (same behavior as the
 * API-route `getCurrentTenant` to keep the two in sync).
 */
export async function requireTenantServer(): Promise<Tenant> {
  const c = await cookies();
  const sessionValue = c.get(SESSION_COOKIE)?.value;
  if (!sessionValue) redirect("/login");

  const secret = await getSessionSecret().catch(() => "");
  if (!secret) redirect("/login");

  const session = verifySession(sessionValue, secret);
  if (session === null) redirect("/login");

  const existing = await db
    .select()
    .from(tenants)
    .where(eq(tenants.authentikSub, session.sub))
    .limit(1);
  if (existing.length > 0) return existing[0];

  // First-sight auto-create. onConflictDoNothing handles the race
  // between two parallel requests for a brand-new sub.
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
    throw new Error("tenant insert succeeded but row not found");
  }
  return created[0];
}
