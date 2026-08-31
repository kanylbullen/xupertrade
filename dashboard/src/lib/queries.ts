import {
  db,
  trades,
  positions,
  equitySnapshots,
  strategyConfigs,
  fundingPayments,
  backtestRuns,
} from "./db";
import { desc, eq, and, gte, lt, sql, sum, count } from "drizzle-orm";

import { type Mode } from "./mode";

// Re-export from the shared mode module so callers can keep importing
// `Mode` from queries.ts unchanged (Copilot review fix on PR #105 —
// dedup of inlined Mode type).
export { type Mode };

// Every query takes `tenantId` as a required parameter. The dashboard
// connects as the postgres superuser (per the Phase 6c PR δ plan
// amendment — the tenant-role pool needs password persistence which is
// out of scope for the closed-beta launch), so RLS is NOT enforced
// here. Every WHERE clause must include `tenant_id = ?` or it leaks
// across tenants. New query functions must follow the same pattern.
// Drizzle's schema marks `tenantId` as notNull on every data table,
// so a missing tenantId arg is a TypeScript error.

export async function getRecentTrades(
  tenantId: string,
  limit = 50,
  mode?: Mode,
) {
  // `mode` is optional after the sidebar cutover: the Trades page is
  // mode-agnostic by default ("all modes") and only narrows when the
  // operator picks a specific mode in the filter pill. The Overview
  // page still passes a concrete mode (route-bound).
  const conditions = [eq(trades.tenantId, tenantId)];
  if (mode !== undefined) {
    conditions.push(eq(trades.mode, mode));
  }
  return db
    .select()
    .from(trades)
    .where(and(...conditions))
    .orderBy(desc(trades.timestamp))
    .limit(limit);
}

/**
 * Filters accepted by the Trades page. Every field is optional; an
 * omitted field means "no constraint on this column".
 *
 * `to` is treated as EXCLUSIVE (`timestamp < to`). The page passes the
 * day *after* the operator's chosen end date, so picking
 * 2026-07-01..2026-07-01 returns that whole day rather than only the
 * single instant at midnight.
 */
export type TradeFilters = {
  mode?: Mode;
  strategy?: string;
  from?: Date;
  to?: Date;
};

function tradeFilterConditions(tenantId: string, f: TradeFilters) {
  const conditions = [eq(trades.tenantId, tenantId)];
  if (f.mode !== undefined) conditions.push(eq(trades.mode, f.mode));
  if (f.strategy !== undefined) {
    conditions.push(eq(trades.strategyName, f.strategy));
  }
  if (f.from !== undefined) conditions.push(gte(trades.timestamp, f.from));
  if (f.to !== undefined) conditions.push(lt(trades.timestamp, f.to));
  return conditions;
}

/**
 * One page of trades matching `filters`, newest first.
 *
 * Ordered by `(timestamp DESC, id DESC)` rather than timestamp alone.
 * Trades written in the same tick share a timestamp, and an unstable
 * sort under OFFSET can drop or duplicate rows across page boundaries;
 * the serial `id` is a unique tiebreaker that makes paging total.
 */
export async function getTradesPage(
  tenantId: string,
  filters: TradeFilters,
  limit: number,
  offset: number,
) {
  return db
    .select()
    .from(trades)
    .where(and(...tradeFilterConditions(tenantId, filters)))
    .orderBy(desc(trades.timestamp), desc(trades.id))
    .limit(limit)
    .offset(offset);
}

/** Total row count for the same filters — drives the pager. */
export async function countTrades(
  tenantId: string,
  filters: TradeFilters,
): Promise<number> {
  const [row] = await db
    .select({ n: count() })
    .from(trades)
    .where(and(...tradeFilterConditions(tenantId, filters)));
  return row?.n ?? 0;
}

/**
 * Distinct strategy names this tenant has ever traded, for the filter
 * dropdown. Deliberately NOT the registry list: the point is to offer
 * only values that can actually return rows, so the operator can't pick
 * a strategy and get an empty table. Not mode-scoped — narrowing it per
 * mode would make options appear and vanish as the mode pill changes.
 */
export async function getTradedStrategyNames(
  tenantId: string,
): Promise<string[]> {
  const rows = await db
    .selectDistinct({ name: trades.strategyName })
    .from(trades)
    .where(eq(trades.tenantId, tenantId))
    .orderBy(trades.strategyName);
  return rows.map((r) => r.name);
}

export async function getOpenPositions(tenantId: string, mode: Mode = "paper") {
  return db
    .select()
    .from(positions)
    .where(
      and(
        eq(positions.tenantId, tenantId),
        eq(positions.isOpen, true),
        eq(positions.mode, mode),
      ),
    );
}

export async function getClosedPositions(
  tenantId: string,
  limit = 50,
  mode: Mode = "paper",
) {
  return db
    .select()
    .from(positions)
    .where(
      and(
        eq(positions.tenantId, tenantId),
        eq(positions.isOpen, false),
        eq(positions.mode, mode),
      ),
    )
    .orderBy(desc(positions.closedAt))
    .limit(limit);
}

export async function getEquityHistory(
  tenantId: string,
  limit = 200,
  mode: Mode = "paper",
) {
  return db
    .select()
    .from(equitySnapshots)
    .where(
      and(
        eq(equitySnapshots.tenantId, tenantId),
        eq(equitySnapshots.mode, mode),
      ),
    )
    .orderBy(desc(equitySnapshots.timestamp))
    .limit(limit);
}

export async function getStrategyConfigs(tenantId: string) {
  return db
    .select()
    .from(strategyConfigs)
    .where(eq(strategyConfigs.tenantId, tenantId))
    .orderBy(strategyConfigs.name);
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
  tenantId: string,
  mode: Mode = "paper",
  sinceDays: number | null = null,
): Promise<StrategyPnl[]> {
  const conditions = [eq(trades.tenantId, tenantId), eq(trades.mode, mode)];
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
  tenantId: string,
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
    .where(
      and(
        eq(trades.tenantId, tenantId),
        eq(trades.mode, mode),
        gte(trades.timestamp, since),
      ),
    )
    .groupBy(sql`to_char(${trades.timestamp}, 'YYYY-MM-DD')`);

  // Funding aggregated by date (separate query — different table)
  const fundingRows = await db
    .select({
      date: sql<string>`to_char(${fundingPayments.timestamp}, 'YYYY-MM-DD')`,
      funding: sql<number>`coalesce(sum(${fundingPayments.usdc}), 0)`,
    })
    .from(fundingPayments)
    .where(
      and(
        eq(fundingPayments.tenantId, tenantId),
        eq(fundingPayments.mode, mode),
        gte(fundingPayments.timestamp, since),
      ),
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

export async function getRealizedPnlTotal(
  tenantId: string,
  mode: Mode = "paper",
): Promise<{
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
    .where(and(eq(trades.tenantId, tenantId), eq(trades.mode, mode)));
  const r = rows[0] ?? { realizedPnl: 0, fees: 0, trades: 0 };
  return {
    realizedPnl: Number(r.realizedPnl),
    fees: Number(r.fees),
    trades: Number(r.trades),
  };
}

export async function getFundingTotal(
  tenantId: string,
  mode: Mode = "paper",
  sinceDays: number | null = null,
): Promise<{ totalUsdc: number; count: number }> {
  const conditions = [
    eq(fundingPayments.tenantId, tenantId),
    eq(fundingPayments.mode, mode),
  ];
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

export async function getLatestEquity(tenantId: string, mode: Mode = "paper") {
  const rows = await db
    .select()
    .from(equitySnapshots)
    .where(
      and(
        eq(equitySnapshots.tenantId, tenantId),
        eq(equitySnapshots.mode, mode),
      ),
    )
    .orderBy(desc(equitySnapshots.timestamp))
    .limit(1);
  return rows[0] ?? null;
}

// ─────────────────────────────────────────────────────────────────────
// Backtest runs (bot CLI history — /backtests page)
// ─────────────────────────────────────────────────────────────────────

/**
 * Filters accepted by the Backtests page. Same conventions as
 * TradeFilters: every field optional, `to` EXCLUSIVE (`created_at < to`)
 * — the page passes the day after the operator's chosen end date.
 *
 * The date bounds apply to when the run was recorded (`created_at`),
 * NOT the candle window it covered (`period_start`/`period_end`): the
 * page answers "what did I run lately", not "what ran on this window".
 */
export type BacktestRunFilters = {
  strategy?: string;
  from?: Date;
  to?: Date;
};

function backtestFilterConditions(tenantId: string, f: BacktestRunFilters) {
  const conditions = [eq(backtestRuns.tenantId, tenantId)];
  if (f.strategy !== undefined) {
    conditions.push(eq(backtestRuns.strategyName, f.strategy));
  }
  if (f.from !== undefined) conditions.push(gte(backtestRuns.createdAt, f.from));
  if (f.to !== undefined) conditions.push(lt(backtestRuns.createdAt, f.to));
  return conditions;
}

/**
 * One page of backtest runs matching `filters`, newest first.
 *
 * Same `(created_at DESC, id DESC)` ordering as the Trades page: a
 * `--all` sweep saves one row per strategy in quick succession, so
 * timestamps collide and the serial id is the tiebreaker that keeps
 * OFFSET paging total.
 */
export async function getBacktestRunsPage(
  tenantId: string,
  filters: BacktestRunFilters,
  limit: number,
  offset: number,
) {
  return db
    .select()
    .from(backtestRuns)
    .where(and(...backtestFilterConditions(tenantId, filters)))
    .orderBy(desc(backtestRuns.createdAt), desc(backtestRuns.id))
    .limit(limit)
    .offset(offset);
}

/** Total row count for the same filters — drives the pager. */
export async function countBacktestRuns(
  tenantId: string,
  filters: BacktestRunFilters,
): Promise<number> {
  const [row] = await db
    .select({ n: count() })
    .from(backtestRuns)
    .where(and(...backtestFilterConditions(tenantId, filters)));
  return row?.n ?? 0;
}

/**
 * Distinct strategy names this tenant has saved runs for, for the
 * filter dropdown. Same reasoning as getTradedStrategyNames: offer only
 * values that can actually return rows, not the full registry.
 */
export async function getBacktestStrategyNames(
  tenantId: string,
): Promise<string[]> {
  const rows = await db
    .selectDistinct({ name: backtestRuns.strategyName })
    .from(backtestRuns)
    .where(eq(backtestRuns.tenantId, tenantId))
    .orderBy(backtestRuns.strategyName);
  return rows.map((r) => r.name);
}

/**
 * Full run history for ONE strategy, oldest first — feeds the APR /
 * Sharpe trend chart on /backtests.
 *
 * Not date-filtered on purpose: the trend is about the strategy's
 * whole saved history, including runs older than anything on the
 * current table page. Newest-first query (same stable tiebreaker)
 * with the newest cap applied, then reversed in JS — a ≤200-row
 * reverse is cheaper than maintaining a second ascending query shape.
 */
export async function getBacktestTrend(
  tenantId: string,
  strategy: string,
  limit = 200,
) {
  const rows = await db
    .select()
    .from(backtestRuns)
    .where(
      and(
        eq(backtestRuns.tenantId, tenantId),
        eq(backtestRuns.strategyName, strategy),
      ),
    )
    .orderBy(desc(backtestRuns.createdAt), desc(backtestRuns.id))
    .limit(limit);
  return rows.reverse();
}
