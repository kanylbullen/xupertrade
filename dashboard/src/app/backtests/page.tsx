import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { BacktestRunsTable } from "@/components/backtest-runs-table";
import { BacktestsFilterBar } from "@/components/backtests-filter-bar";
import { BacktestsPager } from "@/components/backtests-pager";
import { BacktestTrendChart } from "@/components/backtest-trend-chart";
import {
  countBacktestRuns,
  getBacktestRunsPage,
  getBacktestStrategyNames,
  getBacktestTrend,
  type BacktestRunFilters,
} from "@/lib/queries";
import { requireTenantServer } from "@/lib/tenant-server";
import { exclusiveEnd, parseDateParam, parsePageParam } from "./filters";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;
// Cap for the trend chart's run history (oldest-first after reversal).
const TREND_CAP = 200;

/**
 * Backtest run history, read-only view over the `backtest_runs` table
 * the bot CLI writes (see CLAUDE.md § 10 "Backtest CLI"). Mirrors the
 * /trades page: URL-driven filters + offset pagination, tenant-scoped
 * queries, DB-down degrades to the empty state instead of a 500.
 */
export default async function BacktestsPage({
  searchParams,
}: {
  searchParams: Promise<{
    strategy?: string;
    from?: string;
    to?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;

  const strategyParam = params.strategy?.trim() ?? "";
  const fromParam = params.from?.trim() ?? "";
  const toParam = params.to?.trim() ?? "";
  const page = parsePageParam(params.page);

  const tenant = await requireTenantServer();

  const filters: BacktestRunFilters = {
    strategy: strategyParam || undefined,
    from: parseDateParam(fromParam),
    to: exclusiveEnd(toParam),
  };

  let runs: Awaited<ReturnType<typeof getBacktestRunsPage>> = [];
  let total = 0;
  let strategies: string[] = [];
  let trend: Awaited<ReturnType<typeof getBacktestTrend>> = [];
  let dbDown = false;

  try {
    [runs, total, strategies] = await Promise.all([
      getBacktestRunsPage(
        tenant.id,
        filters,
        PAGE_SIZE,
        (page - 1) * PAGE_SIZE,
      ),
      countBacktestRuns(tenant.id, filters),
      getBacktestStrategyNames(tenant.id),
    ]);
    // The trend only means something for ONE strategy — mixing
    // strategies into a single APR line is noise — so it renders (and
    // is fetched) only while the strategy filter is active.
    if (filters.strategy) {
      trend = await getBacktestTrend(tenant.id, filters.strategy, TREND_CAP);
    }
  } catch {
    // DB offline — render the empty state rather than a 500.
    dbDown = true;
  }

  const runRows = runs.map((r) => ({
    id: r.id,
    strategyName: r.strategyName,
    symbol: r.symbol,
    timeframe: r.timeframe,
    leverage: r.leverage,
    days: r.days,
    totalReturnPct: r.totalReturnPct,
    apr: r.apr,
    sharpe: r.sharpe,
    maxDrawdownPct: r.maxDrawdownPct,
    numTrades: r.numTrades,
    numRoundTrips: r.numRoundTrips,
    winRate: r.winRate,
    feesPaid: r.feesPaid,
    positionSizeUsd: r.positionSizeUsd,
    feeRate: r.feeRate,
    slippageBps: r.slippageBps,
    createdAt: r.createdAt,
  }));

  const trendData = trend.map((r) => ({
    createdAt: (r.createdAt ?? new Date(0)).toISOString(),
    apr: r.apr * 100,
    sharpe: r.sharpe,
  }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Backtest Runs</h1>
        <p className="text-sm text-muted-foreground">
          One row per CLI backtest (
          <code className="rounded bg-muted px-1 py-0.5 text-xs">
            python -m hypertrade.backtest
          </code>{" "}
          auto-saves; <code className="text-xs">--no-save</code> opts out).
        </p>
      </div>

      <BacktestsFilterBar
        strategies={strategies}
        strategy={strategyParam}
        from={fromParam}
        to={toParam}
      />

      {filters.strategy && !dbDown && (
        <Card>
          <CardHeader>
            <CardTitle>APR / Sharpe over time — {filters.strategy}</CardTitle>
          </CardHeader>
          <CardContent>
            <BacktestTrendChart data={trendData} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>
            {filters.strategy ?? "All strategies"} ({total})
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {dbDown ? (
            <p className="px-4 py-6 text-sm text-muted-foreground">
              Could not reach the database.
            </p>
          ) : (
            <>
              <BacktestRunsTable runs={runRows} />
              <BacktestsPager page={page} pageSize={PAGE_SIZE} total={total} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
