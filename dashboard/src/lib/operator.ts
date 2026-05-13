/**
 * Operator-only auth helper — multi-tenancy Phase 6c.
 *
 * Wraps `requireTenant` with an additional check that the resolved
 * tenant is the operator (the row inserted by Phase 6b backfill, with
 * `is_operator=true`). Used to gate routes that touch shared host
 * infrastructure rather than tenant-scoped data — currently TLS
 * config, eventually `/api/admin/*`.
 */

import {
  getTenantRowBypassActive,
  requireTenant,
  type Tenant,
} from "./tenant";

/**
 * Resolve the calling tenant and assert they're the operator. Throws
 * a 403 Response (not just 401) when authenticated-but-not-operator,
 * so the caller can distinguish "not signed in" from "signed in as a
 * regular tenant".
 *
 * M-2: operators bypass the `is_active` enforcement that
 * `requireTenant` applies to regular tenants. Otherwise a misconfigured
 * `is_active=false` on the operator row would lock the platform out of
 * operator access (TLS config, eventually /api/admin/*) with no in-band
 * way to recover. Look up the row directly first; only when there's no
 * row at all (or the row is non-operator) do we defer to the standard
 * `requireTenant` path.
 */
export async function requireOperator(req: Request): Promise<Tenant> {
  const row = await getTenantRowBypassActive(req);

  if (row !== null) {
    // Strict `=== true` (not just truthy) so any non-boolean — e.g. the
    // string "true" or 1 from a misconfigured backfill or a future ORM
    // change — does NOT grant operator access. The Drizzle column is
    // boolean.notNull().default(false), so production should always
    // serve a real bool, but the strict check costs nothing and turns a
    // silent privilege escalation into a clear 403.
    if (row.isOperator === true) {
      // Operator: bypasses `is_active` so a fat-fingered disable on the
      // operator row can't lock recovery routes out.
      return row;
    }
    // Non-operator tenant. Distinguish disabled (clearer error message)
    // from active-but-not-operator. Both 403, different error codes.
    // Avoids re-running requireTenant entirely (Copilot review fix on
    // PR #93 — was double-fetching session + tenant row).
    if (row.isActive !== true) {
      throw new Response(
        JSON.stringify({ error: "tenant-disabled" }),
        {
          status: 403,
          headers: { "content-type": "application/json" },
        },
      );
    }
    throw new Response(
      JSON.stringify({ error: "operator only" }),
      {
        status: 403,
        headers: { "content-type": "application/json" },
      },
    );
  }

  // No session OR no tenant row yet (first-sight). Defer to
  // requireTenant which will throw 401 (no session) or auto-create.
  // Auto-created rows are never operators, so we always end up
  // throwing 403 below.
  const t = await requireTenant(req);
  if (t.isOperator !== true) {
    throw new Response(
      JSON.stringify({ error: "operator only" }),
      {
        status: 403,
        headers: { "content-type": "application/json" },
      },
    );
  }
  // Reachable only if a parallel write flipped isOperator=true between
  // the bypass-active lookup and requireTenant. Return for safety.
  return t;
}
