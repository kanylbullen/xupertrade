/**
 * POST /api/tenant/me/bots/[id]/reconcile
 *
 * Operator-triggered backfill: pull HL fill history for the running
 * bot's account and INSERT any missing rows into `trades` (PR #107
 * follow-up). Proxies to the bot's `/api/control/reconcile` endpoint
 * which holds the in-process exchange credentials. Body: optional
 * `{ since_ms: number }` forwarded verbatim.
 */

import { and, eq } from "drizzle-orm";

import { getBotApiUrl } from "@/lib/bot-api";
import { db, tenantBots } from "@/lib/db";
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
  if (!bot.isRunning) {
    return Response.json(
      { error: "bot must be running to reconcile fills" },
      { status: 409 },
    );
  }
  const base = getBotApiUrl(bot);
  if (!base) {
    return Response.json(
      { error: "bot has no resolvable container" },
      { status: 404 },
    );
  }

  // Forward body as-is (caller may pass {since_ms}).
  let bodyText = "";
  try {
    bodyText = await req.text();
  } catch {
    bodyText = "";
  }

  const apiKey = process.env.API_KEY || "";
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (apiKey) headers["X-Api-Key"] = apiKey;

  try {
    const res = await fetch(`${base}/api/control/reconcile`, {
      method: "POST",
      cache: "no-store",
      headers,
      body: bodyText || "{}",
    });
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      const data = await res.json().catch(() => ({}));
      return Response.json(data, { status: res.status });
    }
    const text = await res.text().catch(() => "");
    return new Response(text, {
      status: res.status,
      headers: ct ? { "content-type": ct } : undefined,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "unknown error";
    console.warn("[reconcile] bot fetch failed", { botId, error: message });
    return Response.json(
      { error: "bot unreachable" },
      { status: 502 },
    );
  }
}
