import { and, count, desc, eq, gte, sql } from "drizzle-orm";

import {
  db,
  tenants,
  tenantBots,
  trades,
} from "@/lib/db";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

export async function GET(req: Request): Promise<Response> {
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const tenantRows = await db.select().from(tenants).orderBy(tenants.createdAt);

  // Aggregate bot + trade counts in one round-trip per metric rather
  // than N queries per tenant — for ~10s of tenants this is irrelevant
  // but the shape stays sane as the table grows.
  const sevenDaysAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000);

  const botCounts = await db
    .select({
      tenantId: tenantBots.tenantId,
      total: count(),
      running: sql<number>`SUM(CASE WHEN ${tenantBots.isRunning} THEN 1 ELSE 0 END)`.as(
        "running",
      ),
      lastSeen: sql<Date | null>`MAX(${tenantBots.lastStartedAt})`.as(
        "last_seen",
      ),
    })
    .from(tenantBots)
    .groupBy(tenantBots.tenantId);
  const botByTenant = new Map(botCounts.map((r) => [r.tenantId, r]));

  const tradeAgg = await db
    .select({
      tenantId: trades.tenantId,
      n: count(),
      pnl: sql<number>`COALESCE(SUM(${trades.pnl}), 0)`.as("pnl"),
    })
    .from(trades)
    .where(gte(trades.timestamp, sevenDaysAgo))
    .groupBy(trades.tenantId);
  const tradeByTenant = new Map(tradeAgg.map((r) => [r.tenantId, r]));

  const out = tenantRows.map((t) => {
    const b = botByTenant.get(t.id);
    const tr = tradeByTenant.get(t.id);
    return {
      id: t.id,
      email: t.email,
      displayName: t.displayName,
      isOperator: t.isOperator,
      isActive: t.isActive,
      createdAt: t.createdAt,
      lastSeenAt: b?.lastSeen ?? null,
      activeBotsCount: Number(b?.running ?? 0),
      totalBotsCount: Number(b?.total ?? 0),
      trades7d: Number(tr?.n ?? 0),
      pnl7d: Number(tr?.pnl ?? 0),
      limits: {
        maxActiveBots: t.maxActiveBots,
        maxActiveStrategies: t.maxActiveStrategies,
        allowedStrategies: t.allowedStrategies,
      },
    };
  });

  return Response.json({ tenants: out });
}
