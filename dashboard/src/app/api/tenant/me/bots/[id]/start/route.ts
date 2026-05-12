/**
 * POST /api/tenant/me/bots/[id]/start
 *
 * For an existing bot row whose container is stopped (or was never
 * created), re-decrypt tenant_secrets and start a fresh container.
 * Updates the row with the new containerId. 401 if locked; 409 if
 * the bot is already running.
 *
 * Stop+restart pattern: clients call POST /stop then POST /start
 * rather than a single /restart, because between the two the user
 * may want to swap secrets / rotate keys / etc.
 */

import { and, eq } from "drizzle-orm";

import { db, tenantBots } from "@/lib/db";
import { type BotMode, isValidMode } from "@/lib/bot-orchestrator";
import { requireTenant } from "@/lib/tenant";

import { decryptAndStart } from "../../_decrypt-and-start";

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
  if (bot.isRunning) {
    return Response.json(
      { error: "bot is already running — stop it first" },
      { status: 409 },
    );
  }
  if (!isValidMode(bot.mode)) {
    // Defensive — mode is enforced at the DB row level, but the
    // type narrowing below depends on it.
    return Response.json(
      { error: `bot row has invalid mode '${bot.mode}'` },
      { status: 500 },
    );
  }

  const result = await decryptAndStart({
    req,
    tenant,
    botId,
    mode: bot.mode as BotMode,
  });
  if (result.kind === "response") return result.response;
  return Response.json({ bot: result.bot });
}
