/**
 * /api/tenant/me/bots/[id] — per-bot operations.
 *
 *   GET    → status (DB row + live docker info)
 *   DELETE → stop container + delete DB row (frees the (tenant, mode) slot)
 */

import { and, eq, sql } from "drizzle-orm";

import { db, tenantBots } from "@/lib/db";
import { statusBot, stopBot } from "@/lib/bot-orchestrator";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

async function loadOwnedBot(
  tenantId: string,
  botId: string,
): Promise<typeof tenantBots.$inferSelect | null> {
  const rows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenantId), eq(tenantBots.id, botId)))
    .limit(1);
  return rows[0] ?? null;
}

export async function GET(req: Request, ctx: Params): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { id: botId } = await ctx.params;
  const bot = await loadOwnedBot(tenant.id, botId);
  if (bot === null) {
    return Response.json({ error: "bot not found" }, { status: 404 });
  }

  // Best-effort live status — if the container was removed out from
  // under us (or never started), reconcile DB state to match.
  let live = null;
  if (bot.containerId !== null) {
    try {
      live = await statusBot(bot.containerId);
    } catch (err) {
      const message = err instanceof Error ? err.message : "unknown error";
      return Response.json(
        { bot, docker_error: message },
        { status: 200 },
      );
    }
    if (live === null && bot.isRunning) {
      // Container is gone but DB still says running — reconcile.
      // Defense-in-depth: scope the UPDATE by tenant_id too, even
      // though `loadOwnedBot` already verified ownership. If a
      // future bug bypasses or rewrites ownership checks, this WHERE
      // still prevents a cross-tenant write.
      await db
        .update(tenantBots)
        .set({
          isRunning: false,
          containerId: null,
          lastStoppedAt: sql`now()`,
        })
        .where(
          and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
        );
    }
  }

  return Response.json({ bot, container: live });
}

export async function DELETE(req: Request, ctx: Params): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { id: botId } = await ctx.params;
  const bot = await loadOwnedBot(tenant.id, botId);
  if (bot === null) {
    return Response.json({ error: "bot not found" }, { status: 404 });
  }

  if (bot.containerId !== null) {
    try {
      await stopBot(bot.containerId);
    } catch (err) {
      const message = err instanceof Error ? err.message : "unknown docker error";
      return Response.json(
        { error: `failed to stop container: ${message}` },
        { status: 500 },
      );
    }
  }

  // Defense-in-depth: scope the DELETE by tenant_id even though
  // `loadOwnedBot` already verified ownership above. If a future bug
  // bypasses or rewrites the ownership check, this WHERE still
  // prevents cross-tenant deletion.
  await db
    .delete(tenantBots)
    .where(
      and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
    );
  return Response.json({ id: botId, deleted: true });
}
