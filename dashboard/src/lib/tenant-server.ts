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
  fetchAuthConfig,
  getSessionSecret,
  verifySession,
} from "./auth";
import { randomUUID } from "node:crypto";
import { eq } from "drizzle-orm";
import { getRequiredOidcGroup, type Tenant } from "./tenant";
import { isSessionRevoked } from "./session-store";

// Operator tenant UUID (Phase 6b backfill). Used as a fallback in
// disabled-auth mode (mirrors proxy.ts behavior — proxy lets all
// requests through unauthenticated when cfg.mode === "disabled", so
// server components must also resolve a tenant in that mode rather
// than redirecting to a login that doesn't exist).
const OPERATOR_TENANT_ID = "00000000-0000-0000-0000-000000000001";

/**
 * Resolve the calling tenant from the server-side cookie store.
 * Redirects to /login when there's no/invalid session — server
 * components can't return Responses, so we use Next.js's `redirect()`.
 *
 * Auto-creates the tenant row on first sight (same behavior as the
 * API-route `getCurrentTenant` to keep the two in sync).
 */
export async function requireTenantServer(): Promise<Tenant> {
  // Disabled-auth fast path: mirror proxy.ts which lets all requests
  // through when cfg.mode === "disabled". Without this, pages would
  // redirect to /login even though login is impossible in disabled
  // mode (no auth set up). Resolve the operator tenant — the only
  // sensible identity in a deployment with auth turned off.
  const cfg = await fetchAuthConfig().catch(() => null);
  if (cfg?.mode === "disabled") {
    const op = await db
      .select()
      .from(tenants)
      .where(eq(tenants.id, OPERATOR_TENANT_ID))
      .limit(1);
    if (op.length > 0) return op[0];
    // Operator row missing — Phase 6b never ran, or the row was
    // deleted. Fall through to the cookie path so the user at least
    // sees a clear failure mode.
  }

  const c = await cookies();
  const sessionValue = c.get(SESSION_COOKIE)?.value;
  if (!sessionValue) redirect("/login");

  const secret = await getSessionSecret().catch(() => "");
  if (!secret) redirect("/login");

  const session = verifySession(sessionValue, secret);
  if (session === null) redirect("/login");

  // H-3: revoked cookies bounce to /login same as no/invalid session.
  // Fail-closed on Redis error.
  if (await isSessionRevoked(sessionValue)) redirect("/login");

  const existing = await db
    .select()
    .from(tenants)
    .where(eq(tenants.authentikSub, session.sub))
    .limit(1);
  if (existing.length > 0) {
    // M-2: enforce `is_active`. We deliberately do NOT add this to the
    // WHERE — a filtered query would mask a disabled row as "missing"
    // and silently re-create it via the autoCreate path below. Detect
    // in JS, redirect to /login with a clear error message instead.
    if (existing[0].isActive !== true) redirect("/login?error=tenant-disabled");
    return existing[0];
  }

  // M-3: gate autocreate on the operator-configured Authentik group.
  // Existing tenants above are NOT re-checked — group enforcement is
  // autocreate-only by design. Default-empty `OIDC_REQUIRED_GROUP`
  // preserves pre-M-3 behavior. Mirrors getCurrentTenant in tenant.ts.
  const requiredGroup = getRequiredOidcGroup();
  if (requiredGroup) {
    // Copilot review fix on PR #94: explicit Array.isArray guard
    // mirrors tenant.ts to avoid `.includes` substring-match if
    // `groups` ever lands as a string.
    const groups = Array.isArray(session.groups) ? session.groups : [];
    if (!groups.includes(requiredGroup)) {
      redirect("/login?error=oidc-not-in-required-group");
    }
  }

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
  // M-2 belt-and-braces: pre-existing disabled row could have won the
  // onConflictDoNothing race.
  if (created[0].isActive !== true) redirect("/login?error=tenant-disabled");
  return created[0];
}
