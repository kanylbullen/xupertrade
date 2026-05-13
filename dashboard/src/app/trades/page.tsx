import { permanentRedirect } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TradeTable } from "@/components/trade-table";
import { TradesModeFilter } from "@/components/trades-mode-filter";
import { getRecentTrades } from "@/lib/queries";
import { requireTenantServer } from "@/lib/tenant-server";

export const dynamic = "force-dynamic";

const FILTERS = ["all", "paper", "testnet", "mainnet"] as const;
type Filter = (typeof FILTERS)[number];

function isFilter(v: string | undefined): v is Filter {
  return v === "all" || v === "paper" || v === "testnet" || v === "mainnet";
}

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string; filter?: string }>;
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

  const tenant = await requireTenantServer();

  let trades: Awaited<ReturnType<typeof getRecentTrades>> = [];

  try {
    trades = await getRecentTrades(
      tenant.id,
      100,
      filter === "all" ? undefined : filter,
    );
  } catch {
    // DB offline
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
      <Card>
        <CardHeader>
          <CardTitle>
            {titleMode} ({tradeRows.length})
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <TradeTable trades={tradeRows} />
        </CardContent>
      </Card>
    </div>
  );
}
