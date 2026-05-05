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
import type { PositionRow } from "@/components/position-card";
import {
  StrategyPnlTable,
  DailyPnlTable,
  PnlSummary,
} from "@/components/pnl-breakdown";

export const dynamic = "force-dynamic";

type ExchangePos = {
  symbol: string;
  side: string;
  size: number;
  entry_price: number;
  unrealized_pnl: number;
  liquidation_price?: number | null;
};

export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  const rawMode = params.mode ?? "paper";
  const mode: "paper" | "testnet" | "mainnet" =
    rawMode === "testnet" || rawMode === "mainnet" ? rawMode : "paper";

  const botApiUrls: Record<string, string> = {
    paper: process.env.BOT_API_URL_PAPER ?? "http://bot-paper:8000",
    testnet: process.env.BOT_API_URL_TESTNET ?? "http://bot-testnet:8001",
    mainnet: process.env.BOT_API_URL_MAINNET ?? "http://bot-mainnet:8002",
  };
  const botApiUrl = botApiUrls[mode];

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
      getRecentTrades(20, mode),
      getOpenPositions(mode),
      getEquityHistory(200, mode),
      getLatestEquity(mode),
      getStrategyPnlBreakdown(mode),
      getDailyPnl(mode, 30),
      getRealizedPnlTotal(mode),
      getFundingTotal(mode),
    ]);
    dbConnected = true;
  } catch {
    // DB not available
  }

  // Try to get live exchange positions from bot API (source of truth).
  // Fall back to DB positions if bot is offline.
  let positionRows: PositionRow[] = [];
  let positionsFromExchange = false;

  // /api/positions is auth-gated when API_KEY is set on the bot. Forward
  // the dashboard's API_KEY from server-side env so SSR doesn't 401 and
  // silently degrade to stale DB positions.
  const apiKey = process.env.API_KEY || "";
  const authHeaders: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};

  try {
    const res = await fetch(`${botApiUrl}/api/positions`, { cache: "no-store", headers: authHeaders });
    if (res.ok) {
      const data = await res.json() as { positions: ExchangePos[] };

      // Build a lookup: coin → strategy names from DB
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

  // Fallback: use DB positions (may be stale / not netting correctly)
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
