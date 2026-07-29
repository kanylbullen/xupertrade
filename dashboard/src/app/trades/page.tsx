import { permanentRedirect } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TradeTable } from "@/components/trade-table";
import { TradesModeFilter } from "@/components/trades-mode-filter";
import { TradesFilterBar } from "@/components/trades-filter-bar";
import { TradesPager } from "@/components/trades-pager";
import {
  countTrades,
  getTradedStrategyNames,
  getTradesPage,
  type TradeFilters,
} from "@/lib/queries";
import { requireTenantServer } from "@/lib/tenant-server";
import { exclusiveEnd, parseDateParam, parsePageParam } from "./filters";

export const dynamic = "force-dynamic";

const FILTERS = ["all", "paper", "testnet", "mainnet"] as const;
type Filter = (typeof FILTERS)[number];

const PAGE_SIZE = 50;

function isFilter(v: string | undefined): v is Filter {
  return v === "all" || v === "paper" || v === "testnet" || v === "mainnet";
}

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<{
    mode?: string;
    filter?: string;
    strategy?: string;
    from?: string;
    to?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;

  // Legacy `?mode=foo` bookmarks → 308 to `?filter=foo` so they keep
  // landing on the same dataset under the new query param. The
  // `mode=paper` default the old page used isn't preserved (it would
  // override the new "all-modes" default and break the cutover's
  // mental model); we narrow only when the URL was explicit about a
  // specific mode.
  if (params.mode) {
    const m = params.mode;
    const target = m === "paper" || m === "testnet" || m === "mainnet"
      ? `/trades?filter=${m}`
      : "/trades";
    permanentRedirect(target);
  }

  const filter: Filter = isFilter(params.filter) ? params.filter : "all";
  const strategyParam = params.strategy?.trim() ?? "";
  const fromParam = params.from?.trim() ?? "";
  const toParam = params.to?.trim() ?? "";

  const page = parsePageParam(params.page);

  const tenant = await requireTenantServer();

  const from = parseDateParam(fromParam);
  const to = exclusiveEnd(toParam);

  const filters: TradeFilters = {
    mode: filter === "all" ? undefined : filter,
    strategy: strategyParam || undefined,
    from,
    to,
  };

  let trades: Awaited<ReturnType<typeof getTradesPage>> = [];
  let total = 0;
  let strategies: string[] = [];
  let dbDown = false;

  try {
    [trades, total, strategies] = await Promise.all([
      getTradesPage(tenant.id, filters, PAGE_SIZE, (page - 1) * PAGE_SIZE),
      countTrades(tenant.id, filters),
      getTradedStrategyNames(tenant.id),
    ]);
  } catch {
    // DB offline — render the empty state rather than a 500.
    dbDown = true;
  }

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

  const titleMode =
    filter === "all" ? "All modes" : filter[0].toUpperCase() + filter.slice(1);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold">Trade History</h1>
        <TradesModeFilter active={filter} />
      </div>

      <TradesFilterBar
        strategies={strategies}
        strategy={strategyParam}
        from={fromParam}
        to={toParam}
      />

      <Card>
        <CardHeader>
          <CardTitle>
            {titleMode} ({total})
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {dbDown ? (
            <p className="px-4 py-6 text-sm text-muted-foreground">
              Could not reach the database.
            </p>
          ) : (
            <>
              <TradeTable trades={tradeRows} />
              <TradesPager page={page} pageSize={PAGE_SIZE} total={total} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
