import { db, trades, positions, equitySnapshots, strategyConfigs, fundingPayments } from "./db";
import { desc, eq, and, gte, sql, sum, count } from "drizzle-orm";

export type Mode = "paper" | "testnet" | "mainnet";

export async function getRecentTrades(limit = 50, mode: Mode = "paper") {
  return db
    .select()
    .from(trades)
    .where(eq(trades.mode, mode))
    .orderBy(desc(trades.timestamp))
    .limit(limit);
}

export async function getOpenPositions(mode: Mode = "paper") {
  return db
    .select()
    .from(positions)
    .where(and(eq(positions.isOpen, true), eq(positions.mode, mode)));
}

export async function getClosedPositions(limit = 50, mode: Mode = "paper") {
  return db
    .select()
    .from(positions)
    .where(and(eq(positions.isOpen, false), eq(positions.mode, mode)))
    .orderBy(desc(positions.closedAt))
    .limit(limit);
}

export async function getEquityHistory(limit = 200, mode: Mode = "paper") {
  return db
    .select()
    .from(equitySnapshots)
    .where(eq(equitySnapshots.mode, mode))
    .orderBy(desc(equitySnapshots.timestamp))
    .limit(limit);
}

export async function getStrategyConfigs() {
  return db.select().from(strategyConfigs).orderBy(strategyConfigs.name);
}

export type StrategyPnl = {
  strategyName: string;
  trades: number;
  wins: number;
  losses: number;
  realizedPnl: number;
  fees: number;
};

export async function getStrategyPnlBreakdown(
  mode: Mode = "paper",
  sinceDays: number | null = null,
): Promise<StrategyPnl[]> {
  const conditions = [eq(trades.mode, mode)];
  if (sinceDays !== null) {
    const since = new Date(Date.now() - sinceDays * 24 * 60 * 60 * 1000);
    conditions.push(gte(trades.timestamp, since));
  }
  const rows = await db
    .select({
      strategyName: trades.strategyName,
      trades: count(trades.id),
      realizedPnl: sql<number>`coalesce(sum(${trades.pnl}), 0)`,
      fees: sql<number>`coalesce(sum(${trades.fee}), 0)`,
      wins: sql<number>`coalesce(sum(case when ${trades.pnl} > 0 then 1 else 0 end), 0)`,
      losses: sql<number>`coalesce(sum(case when ${trades.pnl} < 0 then 1 else 0 end), 0)`,
    })
    .from(trades)
    .where(and(...conditions))
    .groupBy(trades.strategyName);
  return rows.map((r) => ({
    strategyName: r.strategyName,
    trades: Number(r.trades),
    wins: Number(r.wins),
    losses: Number(r.losses),
    realizedPnl: Number(r.realizedPnl),
    fees: Number(r.fees),
  }));
}

export type DailyPnl = {
  date: string; // YYYY-MM-DD
  realizedPnl: number;
  fees: number;
  trades: number;
  funding: number; // signed: positive = received, negative = paid
  net: number; // realizedPnl + funding
};

export async function getDailyPnl(
  mode: Mode = "paper",
  days = 30,
): Promise<DailyPnl[]> {
  const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000);

  // Trades aggregated by date
  const tradeRows = await db
    .select({
      date: sql<string>`to_char(${trades.timestamp}, 'YYYY-MM-DD')`,
      realizedPnl: sql<number>`coalesce(sum(${trades.pnl}), 0)`,
      fees: sql<number>`coalesce(sum(${trades.fee}), 0)`,
      trades: count(trades.id),
    })
    .from(trades)
    .where(and(eq(trades.mode, mode), gte(trades.timestamp, since)))
    .groupBy(sql`to_char(${trades.timestamp}, 'YYYY-MM-DD')`);

  // Funding aggregated by date (separate query — different table)
  const fundingRows = await db
    .select({
      date: sql<string>`to_char(${fundingPayments.timestamp}, 'YYYY-MM-DD')`,
      funding: sql<number>`coalesce(sum(${fundingPayments.usdc}), 0)`,
    })
    .from(fundingPayments)
    .where(
      and(eq(fundingPayments.mode, mode), gte(fundingPayments.timestamp, since)),
    )
    .groupBy(sql`to_char(${fundingPayments.timestamp}, 'YYYY-MM-DD')`);

  // Merge by date — union of both date sets
  const byDate = new Map<string, DailyPnl>();
  for (const t of tradeRows) {
    byDate.set(t.date, {
      date: t.date,
      realizedPnl: Number(t.realizedPnl),
      fees: Number(t.fees),
      trades: Number(t.trades),
      funding: 0,
      net: Number(t.realizedPnl),
    });
  }
  for (const f of fundingRows) {
    const fundingNum = Number(f.funding);
    const existing = byDate.get(f.date);
    if (existing) {
      existing.funding = fundingNum;
      existing.net = existing.realizedPnl + fundingNum;
    } else {
      byDate.set(f.date, {
        date: f.date,
        realizedPnl: 0,
        fees: 0,
        trades: 0,
        funding: fundingNum,
        net: fundingNum,
      });
    }
  }

  return Array.from(byDate.values()).sort((a, b) => a.date.localeCompare(b.date));
}

export async function getRealizedPnlTotal(mode: Mode = "paper"): Promise<{
  realizedPnl: number;
  fees: number;
  trades: number;
}> {
  const rows = await db
    .select({
      realizedPnl: sql<number>`coalesce(sum(${trades.pnl}), 0)`,
      fees: sql<number>`coalesce(sum(${trades.fee}), 0)`,
      trades: count(trades.id),
    })
    .from(trades)
    .where(eq(trades.mode, mode));
  const r = rows[0] ?? { realizedPnl: 0, fees: 0, trades: 0 };
  return {
    realizedPnl: Number(r.realizedPnl),
    fees: Number(r.fees),
    trades: Number(r.trades),
  };
}

export async function getFundingTotal(
  mode: Mode = "paper",
  sinceDays: number | null = null,
): Promise<{ totalUsdc: number; count: number }> {
  const conditions = [eq(fundingPayments.mode, mode)];
  if (sinceDays !== null) {
    const since = new Date(Date.now() - sinceDays * 24 * 60 * 60 * 1000);
    conditions.push(gte(fundingPayments.timestamp, since));
  }
  const rows = await db
    .select({
      totalUsdc: sql<number>`coalesce(sum(${fundingPayments.usdc}), 0)`,
      count: count(fundingPayments.id),
    })
    .from(fundingPayments)
    .where(and(...conditions));
  const r = rows[0] ?? { totalUsdc: 0, count: 0 };
  return { totalUsdc: Number(r.totalUsdc), count: Number(r.count) };
}

export async function getLatestEquity(mode: Mode = "paper") {
  const rows = await db
    .select()
    .from(equitySnapshots)
    .where(eq(equitySnapshots.mode, mode))
    .orderBy(desc(equitySnapshots.timestamp))
    .limit(1);
  return rows[0] ?? null;
}
