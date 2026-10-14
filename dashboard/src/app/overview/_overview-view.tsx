import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { OverviewStats } from "@/components/overview-stats";
import { EquityChart } from "@/components/equity-chart";
import { TradeTable } from "@/components/trade-table";
import { TradingViewTicker } from "@/components/tv-chart";
import { PositionList } from "@/components/position-list";
import { IndicatorStatus } from "@/components/indicator-status";
import {
  getRecentTrades,
  getOpenPositions,
  getEquityHistory,
  getLatestEquity,
  getStrategyPnlBreakdown,
  getDailyPnl,
  getRealizedPnlTotal,
  getFundingTotal,
  getFirstEquitySince,
} from "@/lib/queries";
import { formatDateTime } from "@/lib/format";
import { requestNow } from "@/lib/now";
import {
  EQUITY_WINDOWS,
  equityChange,
  todayRow,
  type EquityPoint,
} from "@/lib/overview-numbers";
import { requireTenantServer } from "@/lib/tenant-server";
import { db, tenantBots } from "@/lib/db";
import { getBotApiUrl } from "@/lib/bot-api";
import { loadBotApiKey } from "@/lib/bot-api-key";
import { and, eq } from "drizzle-orm";
import type { PositionRow } from "@/components/position-card";
import {
  StrategyPnlTable,
  DailyPnlTable,
  PnlSummary,
} from "@/components/pnl-breakdown";

export type OverviewMode = "paper" | "testnet" | "mainnet";

type ExchangePos = {
  symbol: string;
  side: string;
  size: number;
  entry_price: number;
  unrealized_pnl: number;
  liquidation_price?: number | null;
};

/**
 * Shared overview body — rendered by both the legacy `?mode=` route
 * (`app/page.tsx`) and the new route-bound `/overview/[mode]` route
 * (`app/overview/[mode]/page.tsx`). Extracted in PR A of the sidebar
 * nav refactor so the route shape can change without duplicating the
 * (sizeable) body. PR C may inline this back if cleaner once
 * `app/page.tsx` is replaced with a redirect.
 */
export async function OverviewView({ mode }: { mode: OverviewMode }) {
  // Resolves the calling tenant or redirects to /login. proxy.ts
  // already gates the page route for unauthenticated users — this is
  // belt-and-braces and gives us the tenant.id for tenant-scoped reads.
  const tenant = await requireTenantServer();

  const botRows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenant.id), eq(tenantBots.mode, mode)))
    .limit(1);
  const botApiUrl = botRows[0] ? getBotApiUrl(botRows[0]) : null;

  let trades: Awaited<ReturnType<typeof getRecentTrades>> = [];
  let dbPositions: Awaited<ReturnType<typeof getOpenPositions>> = [];
  let equityHistory: Awaited<ReturnType<typeof getEquityHistory>> = [];
  let latestEquityRow: Awaited<ReturnType<typeof getLatestEquity>> | null = null;
  let strategyPnl: Awaited<ReturnType<typeof getStrategyPnlBreakdown>> = [];
  let dailyPnl: Awaited<ReturnType<typeof getDailyPnl>> = [];
  let realizedTotal = { realizedPnl: 0, fees: 0, entryFees: 0, trades: 0 };
  let fundingTotal = { totalUsdc: 0, count: 0 };
  let equityBaselines: Array<Awaited<ReturnType<typeof getFirstEquitySince>>> = [];
  let dbConnected = false;

  const nowMs = requestNow();
  const DAILY_WINDOW_DAYS = 30;

  try {
    [
      trades,
      dbPositions,
      equityHistory,
      latestEquityRow,
      strategyPnl,
      dailyPnl,
      realizedTotal,
      fundingTotal,
      equityBaselines,
    ] = await Promise.all([
      getRecentTrades(tenant.id, 20, mode),
      getOpenPositions(tenant.id, mode),
      getEquityHistory(tenant.id, 200, mode),
      getLatestEquity(tenant.id, mode),
      getStrategyPnlBreakdown(tenant.id, mode),
      getDailyPnl(tenant.id, mode, DAILY_WINDOW_DAYS),
      getRealizedPnlTotal(tenant.id, mode),
      getFundingTotal(tenant.id, mode),
      Promise.all(
        EQUITY_WINDOWS.map((w) =>
          getFirstEquitySince(tenant.id, mode, new Date(nowMs - w.ms)),
        ),
      ),
    ]);
    dbConnected = true;
  } catch {
    // DB not available
  }

  let positionRows: PositionRow[] = [];
  let positionsFromExchange = false;

  // Per-bot API key (security audit H-1) — null when no bot row.
  const apiKey = botRows[0] ? (await loadBotApiKey(botRows[0].id)) || "" : "";
  const authHeaders: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};

  if (!botApiUrl) {
    // no bot yet — leave positionsFromExchange=false so DB fallback runs
  } else try {
    const res = await fetch(`${botApiUrl}/api/positions`, { cache: "no-store", headers: authHeaders });
    if (res.ok) {
      const data = await res.json() as { positions: ExchangePos[] };

      const coinToStrategies: Record<string, string[]> = {};
      for (const p of dbPositions) {
        const arr = coinToStrategies[p.symbol] ?? [];
        arr.push(p.strategyName);
        coinToStrategies[p.symbol] = arr;
      }

      positionRows = data.positions.map((p) => ({
        symbol: p.symbol,
        side: p.side,
        size: p.size,
        entryPrice: p.entry_price,
        unrealizedPnl: p.unrealized_pnl,
        liquidationPrice: p.liquidation_price,
        strategies: coinToStrategies[p.symbol] ?? [],
        source: "exchange" as const,
      }));
      positionsFromExchange = true;
    }
  } catch {
    // bot offline
  }

  if (!positionsFromExchange) {
    positionRows = dbPositions.map((p) => ({
      symbol: p.symbol,
      side: p.side,
      size: p.size,
      entryPrice: p.entryPrice,
      unrealizedPnl: p.pnl ?? 0,
      strategies: [p.strategyName],
      source: "db" as const,
    }));
  }

  const equityData = equityHistory
    .reverse()
    .map((e) => ({
      timestamp: e.timestamp?.toISOString() ?? "",
      totalEquity: e.totalEquity,
    }));

  // No snapshot means no number: the cards show an empty state rather
  // than a $10,000 placeholder.
  const toPoint = (
    row: { totalEquity: number; timestamp: Date | null } | null,
  ): EquityPoint | null =>
    row?.timestamp ? { equity: row.totalEquity, at: row.timestamp } : null;
  const latestEquity = toPoint(latestEquityRow);
  const equityChanges = EQUITY_WINDOWS.map((w, i) =>
    equityChange(w.label, w.ms, latestEquity, toPoint(equityBaselines[i] ?? null), nowMs),
  );
  const unrealizedPnl = positionRows.reduce((s, p) => s + (p.unrealizedPnl ?? 0), 0);

  const tradeRows = trades.map((t) => ({
    id: t.id,
    strategyName: t.strategyName,
    symbol: t.symbol,
    side: t.side,
    size: t.size,
    price: t.price,
    fee: t.fee,
    pnl: t.pnl,
    reason: t.reason,
    timestamp: t.timestamp,
  }));

  return (
    <div className="space-y-6">
      <TradingViewTicker symbols={["BTC", "ETH", "SOL"]} />

      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">Overview</h1>
        {!dbConnected && (
          <span className="text-xs text-yellow-500 border border-yellow-500/30 px-2 py-0.5 rounded">
            DB offline
          </span>
        )}
        {!positionsFromExchange && dbConnected && (
          <span className="text-xs text-yellow-500 border border-yellow-500/30 px-2 py-0.5 rounded">
            Bot offline — positions from DB
          </span>
        )}
      </div>

      <OverviewStats
        modeLabel={mode === "paper" ? "Paper trading" : mode === "testnet" ? "Testnet (live)" : "Mainnet (live)"}
        dbConnected={dbConnected}
        latestEquity={latestEquity}
        equityChanges={equityChanges}
        realized={realizedTotal}
        today={todayRow(dailyPnl, nowMs)}
        nowMs={nowMs}
      />

      <PnlSummary
        realized={realizedTotal.realizedPnl}
        entryFees={realizedTotal.entryFees}
        funding={fundingTotal.totalUsdc}
        unrealized={unrealizedPnl}
        dbConnected={dbConnected}
      />

      <div className="grid gap-6 lg:grid-cols-2">
        <StrategyPnlTable rows={strategyPnl} dbConnected={dbConnected} />
        <DailyPnlTable
          rows={dailyPnl}
          windowDays={DAILY_WINDOW_DAYS}
          dbConnected={dbConnected}
        />
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Strategy Signal Status</h2>
        <IndicatorStatus mode={mode} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Equity Curve</CardTitle>
          {equityData.length > 0 && equityData[0].timestamp && (
            <p className="text-xs text-muted-foreground">
              Latest {equityData.length} snapshots, since{" "}
              {formatDateTime(equityData[0].timestamp)}
            </p>
          )}
        </CardHeader>
        <CardContent>
          <EquityChart data={equityData} />
        </CardContent>
      </Card>

      <div className="space-y-3">
        <PositionList positions={positionRows} />
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Recent Trades</h2>
        <Card>
          <CardContent className="p-0">
            <TradeTable trades={tradeRows} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
