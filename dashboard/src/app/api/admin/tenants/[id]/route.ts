import { and, count, desc, eq, gte, sql } from "drizzle-orm";

import { db, tenants, tenantBots, trades } from "@/lib/db";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

export async function GET(req: Request, ctx: Params): Promise<Response> {
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const { id } = await ctx.params;
  const rows = await db
    .select()
    .from(tenants)
    .where(eq(tenants.id, id))
    .limit(1);
  const t = rows[0];
  if (!t) return Response.json({ error: "tenant not found" }, { status: 404 });

  const bots = await db
    .select()
    .from(tenantBots)
    .where(eq(tenantBots.tenantId, id))
    .orderBy(tenantBots.mode);

  const now = Date.now();
  const window = (days: number) =>
    db
      .select({
        n: count(),
        pnl: sql<number>`COALESCE(SUM(${trades.pnl}), 0)`.as("pnl"),
      })
      .from(trades)
      .where(
        and(
          eq(trades.tenantId, id),
          gte(trades.timestamp, new Date(now - days * 24 * 60 * 60 * 1000)),
        ),
      );

  const allWindow = db
    .select({
      n: count(),
      pnl: sql<number>`COALESCE(SUM(${trades.pnl}), 0)`.as("pnl"),
    })
    .from(trades)
    .where(eq(trades.tenantId, id));

  const [pnl7, pnl30, pnlAll] = await Promise.all([window(7), window(30), allWindow]);

  const recentTrades = await db
    .select()
    .from(trades)
    .where(eq(trades.tenantId, id))
    .orderBy(desc(trades.timestamp))
    .limit(20);

  return Response.json({
    tenant: {
      id: t.id,
      email: t.email,
      displayName: t.displayName,
      isOperator: t.isOperator,
      isActive: t.isActive,
      createdAt: t.createdAt,
      multiBotEnabled: t.multiBotEnabled,
      limits: {
        maxActiveBots: t.maxActiveBots,
        maxActiveStrategies: t.maxActiveStrategies,
        allowedStrategies: t.allowedStrategies,
      },
    },
    bots,
    pnl: {
      "7d": { trades: Number(pnl7[0].n), pnl: Number(pnl7[0].pnl) },
      "30d": { trades: Number(pnl30[0].n), pnl: Number(pnl30[0].pnl) },
      all: { trades: Number(pnlAll[0].n), pnl: Number(pnlAll[0].pnl) },
    },
    recentTrades,
  });
}
