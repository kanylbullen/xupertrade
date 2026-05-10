/**
 * GET /api/tenant/me/secrets — list this tenant's stored secret keys.
 *
 * No values returned — just the keys (e.g. ['HYPERLIQUID_PRIVATE_KEY',
 * 'TELEGRAM_BOT_TOKEN']) plus an `updatedAt` timestamp per row.
 * Doesn't require unlock; lets the settings UI show "set / not set"
 * status without prompting for the passphrase.
 */

import { eq } from "drizzle-orm";

import { db, tenantSecrets } from "@/lib/db";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

export async function GET(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const rows = await db
    .select({ key: tenantSecrets.key, updatedAt: tenantSecrets.updatedAt })
    .from(tenantSecrets)
    .where(eq(tenantSecrets.tenantId, tenant.id))
    .orderBy(tenantSecrets.key);

  return Response.json({ secrets: rows });
}
