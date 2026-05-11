/**
 * Operator-only auth helper — multi-tenancy Phase 6c.
 *
 * Wraps `requireTenant` with an additional check that the resolved
 * tenant is the operator (the row inserted by Phase 6b backfill, with
 * `is_operator=true`). Used to gate routes that touch shared host
 * infrastructure rather than tenant-scoped data — currently TLS
 * config, eventually `/api/admin/*`.
 */

import { requireTenant, type Tenant } from "./tenant";

/**
 * Resolve the calling tenant and assert they're the operator. Throws
 * a 403 Response (not just 401) when authenticated-but-not-operator,
 * so the caller can distinguish "not signed in" from "signed in as a
 * regular tenant".
 */
export async function requireOperator(req: Request): Promise<Tenant> {
  const t = await requireTenant(req); // throws 401 if no session
  // Strict `!== true` (not `!t.isOperator`) so any truthy non-boolean
  // — e.g. the string "true" or 1 from a misconfigured backfill or a
  // future ORM change — does NOT grant operator access. The Drizzle
  // column is boolean.notNull().default(false), so production should
  // always serve a real bool, but the strict check costs nothing and
  // turns a silent privilege escalation into a clear 403.
  if (t.isOperator !== true) {
    throw new Response(
      JSON.stringify({ error: "operator only" }),
      {
        status: 403,
        headers: { "content-type": "application/json" },
      },
    );
  }
  return t;
}
