/**
 * POST /api/tenant/me/bots/[id]/stop
 *
 * Stop the running container but keep the DB row so the user can
 * restart later via POST /start without recreating from scratch
 * (preserves the (tenant, mode) slot).
 *
 * Idempotent: if the row says isRunning=false or has no
 * containerId, return 200 without touching docker. If docker says
 * the container is already gone, treat as success and reconcile.
 */

import { and, eq, sql } from "drizzle-orm";

import { db, tenantBots } from "@/lib/db";
import { stopBot } from "@/lib/bot-orchestrator";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

export async function POST(req: Request, ctx: Params): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { id: botId } = await ctx.params;
  const rows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)))
    .limit(1);
  const bot = rows[0];
  if (!bot) {
    return Response.json({ error: "bot not found" }, { status: 404 });
  }

  // Already stopped — nothing to do, just return current state.
  if (!bot.isRunning && bot.containerId === null) {
    return Response.json({ bot });
  }

  // Stop the container if we have a handle on one. stopBot() is
  // idempotent on already-gone containers.
  if (bot.containerId !== null) {
    try {
      await stopBot(bot.containerId);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "unknown docker error";
      return Response.json(
        { error: `failed to stop container: ${message}` },
        { status: 500 },
      );
    }
  }

  // Reconcile DB row regardless — container is either stopped or
  // was already gone. Defense-in-depth: scope by tenant_id even
  // though we already verified ownership.
  const updated = await db
    .update(tenantBots)
    .set({
      isRunning: false,
      containerId: null,
      // Clear container_name too — the container is gone after
      // stopAndRemove(), so the routing target is no longer valid.
      // Keeping a stale name here would let `getBotApiUrl` resolve to
      // a hostname that no longer exists, masking real failures.
      containerName: null,
      lastStoppedAt: sql`now()`,
    })
    .where(
      and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
    )
    .returning();

  return Response.json({ bot: updated[0] });
}
