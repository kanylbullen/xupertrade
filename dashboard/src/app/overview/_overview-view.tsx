import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/stat-card";
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
} from "@/lib/queries";
import { requireTenantServer } from "@/lib/tenant-server";
import { db, tenantBots } from "@/lib/db";
import { getBotApiUrl } from "@/lib/bot-api";
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
  let realizedTotal = { realizedPnl: 0, fees: 0, trades: 0 };
  let fundingTotal = { totalUsdc: 0, count: 0 };
  let dbConnected = false;

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
    ] = await Promise.all([
      getRecentTrades(tenant.id, 20, mode),
      getOpenPositions(tenant.id, mode),
      getEquityHistory(tenant.id, 200, mode),
      getLatestEquity(tenant.id, mode),
      getStrategyPnlBreakdown(tenant.id, mode),
      getDailyPnl(tenant.id, mode, 30),
      getRealizedPnlTotal(tenant.id, mode),
      getFundingTotal(tenant.id, mode),
    ]);
    dbConnected = true;
  } catch {
    // DB not available
  }

  let positionRows: PositionRow[] = [];
  let positionsFromExchange = false;

  const apiKey = process.env.API_KEY || "";
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

  const totalEquity = latestEquityRow?.totalEquity ?? 10_000;
  const startEquity = equityData.length > 0 ? equityData[0].totalEquity : 10_000;
  const totalPnl = totalEquity - startEquity;
  const totalPnlPct = startEquity > 0 ? (totalPnl / startEquity) * 100 : 0;
  const unrealizedPnl = positionRows.reduce((s, p) => s + (p.unrealizedPnl ?? 0), 0);
  const todayPnl = dailyPnl.length > 0
    ? dailyPnl[dailyPnl.length - 1].realizedPnl
    : 0;

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

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="Total Equity"
          value={`$${totalEquity.toLocaleString()}`}
          subtitle={mode === "paper" ? "Paper trading" : mode === "testnet" ? "Testnet (live)" : "Mainnet (live)"}
          trend="up"
        />
        <StatCard
          title="Equity P&L"
          value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`}
          subtitle={`${totalPnlPct >= 0 ? "+" : ""}${totalPnlPct.toFixed(2)}% (since first snapshot)`}
          trend={totalPnl >= 0 ? "up" : "down"}
        />
        <StatCard
          title="Realized P&L"
          value={`${realizedTotal.realizedPnl >= 0 ? "+" : ""}$${realizedTotal.realizedPnl.toFixed(2)}`}
          subtitle={`${realizedTotal.trades} trades, $${realizedTotal.fees.toFixed(2)} fees`}
          trend={realizedTotal.realizedPnl >= 0 ? "up" : "down"}
        />
        <StatCard
          title="Today's P&L"
          value={`${todayPnl >= 0 ? "+" : ""}$${todayPnl.toFixed(2)}`}
          subtitle={`Unrealized ${unrealizedPnl >= 0 ? "+" : ""}$${unrealizedPnl.toFixed(2)} | Funding ${fundingTotal.totalUsdc >= 0 ? "+" : ""}$${fundingTotal.totalUsdc.toFixed(2)}`}
          trend={todayPnl >= 0 ? "up" : "down"}
        />
      </div>

      <PnlSummary
        realized={realizedTotal.realizedPnl}
        fees={realizedTotal.fees}
        funding={fundingTotal.totalUsdc}
        unrealized={unrealizedPnl}
      />

      <div className="grid gap-6 lg:grid-cols-2">
        <StrategyPnlTable rows={strategyPnl} />
        <DailyPnlTable rows={dailyPnl} />
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Strategy Signal Status</h2>
        <IndicatorStatus />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Equity Curve</CardTitle>
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
