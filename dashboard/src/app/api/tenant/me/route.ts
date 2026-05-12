import { NextResponse } from "next/server";

import { loadKey } from "@/lib/crypto/k-cache";
import {
  getSessionIdFromRequest,
  requireTenant,
  type Tenant,
} from "@/lib/tenant";

export const dynamic = "force-dynamic";

/**
 * GET /api/tenant/me
 * Returns the calling tenant's identity + onboarding state. Used by
 * the user-menu in the nav AND the credentials wizard to pick which
 * UI branch to render (set-passphrase / unlock / normal).
 *
 *   passphraseSet — tenant has run /api/tenant/me/passphrase once.
 *                   When false, route the user into the wizard.
 *   unlocked      — K is currently cached for this session. When false
 *                   and passphraseSet is true, prompt for passphrase
 *                   before any encrypt/decrypt action.
 *
 * Both flags are cheap-derived from existing state — no new DB
 * columns, no migration. `unlocked` does a single Redis GET via
 * `loadKey`.
 *
 * 401 when there's no/invalid session.
 */
export async function GET(req: Request) {
  let tenant: Tenant;
  try {
    tenant = await requireTenant(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const passphraseSet = tenant.passphraseVerifier !== null;

  let unlocked = false;
  if (passphraseSet) {
    const sessionId = getSessionIdFromRequest(req);
    if (sessionId !== null) {
      const k = await loadKey(tenant.id, sessionId);
      unlocked = k !== null;
    }
  }

  return NextResponse.json({
    id: tenant.id,
    email: tenant.email,
    displayName: tenant.displayName,
    isOperator: tenant.isOperator,
    passphraseSet,
    unlocked,
  });
}
