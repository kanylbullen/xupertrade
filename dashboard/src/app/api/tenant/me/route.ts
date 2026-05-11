import { NextResponse } from "next/server";

import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

/**
 * GET /api/tenant/me
 * Returns the calling tenant's identity (id, email, displayName,
 * isOperator). Used by the user-menu component in the nav to render
 * "signed in as ..." and decide which UI to show.
 *
 * 401 when there's no/invalid session.
 */
export async function GET(req: Request) {
  let tenant;
  try {
    tenant = await requireTenant(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }
  return NextResponse.json({
    id: tenant.id,
    email: tenant.email,
    displayName: tenant.displayName,
    isOperator: tenant.isOperator,
  });
}
