/**
 * POST /api/tenant/me/bots/[id]/start
 *
 * For an existing bot row whose container is stopped (or was never
 * created), re-decrypt tenant_secrets and start a fresh container.
 * Updates the row with the new containerId. 401 if locked; 409 if
 * the bot is already running or losing the start race; 422 if a
 * required secret is missing for this bot's mode.
 *
 * Stop+restart pattern: clients call POST /stop then POST /start
 * rather than a single /restart, because between the two the user
 * may want to swap secrets / rotate keys / etc.
 */

import { and, eq, sql } from "drizzle-orm";

import { db, tenantBots, tenantSecrets } from "@/lib/db";
import {
  type BotMode,
  isValidMode,
  requiredSecretsForMode,
} from "@/lib/bot-orchestrator";
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
  const mode = bot.mode as BotMode;

  // Re-validate required secrets for this mode. The user could
  // have deleted a secret since bot creation; without this, /start
  // would silently start a bot that crashes on first tick. 422
  // matches the create-route's behavior so the UI can surface the
  // missing keys consistently.
  const required = requiredSecretsForMode(mode);
  if (required.length > 0) {
    const presentRows = await db
      .select({ key: tenantSecrets.key })
      .from(tenantSecrets)
      .where(eq(tenantSecrets.tenantId, tenant.id));
    const present = new Set(presentRows.map((r) => r.key));
    const missing = required.filter((k) => !present.has(k));
    if (missing.length > 0) {
      return Response.json(
        {
          error: `missing required secrets for mode=${mode}: ${missing.join(", ")}`,
        },
        { status: 422 },
      );
    }
  }

  // Atomic claim: flip isRunning false→true (with a placeholder
  // containerId='claiming') in a single SQL statement. Two
  // concurrent /start requests would both pass the read above, but
  // only one can win this UPDATE — the other gets 0 rows back and
  // returns 409 instead of starting a duplicate container.
  //
  // The claim placeholder is overwritten by decryptAndStart's
  // final UPDATE with the real container_id; if that fails (or
  // start fails inside the helper), we revert here so the row
  // returns to startable.
  const claimed = await db
    .update(tenantBots)
    .set({
      isRunning: true,
      containerId: "claiming",
      lastStartedAt: sql`now()`,
    })
    .where(
      and(
        eq(tenantBots.id, botId),
        eq(tenantBots.tenantId, tenant.id),
        eq(tenantBots.isRunning, false),
      ),
    )
    .returning({ id: tenantBots.id });
  if (claimed.length === 0) {
    return Response.json(
      { error: "bot is being started by another request" },
      { status: 409 },
    );
  }

  const result = await decryptAndStart({
    req,
    tenant,
    botId,
    mode,
  });
  if (result.kind === "response") {
    // Revert the claim so the row is startable again.
    await db
      .update(tenantBots)
      .set({ isRunning: false, containerId: null })
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      )
      .catch(() => undefined);
    return result.response;
  }
  return Response.json({ bot: result.bot });
}
