import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TradeTable } from "@/components/trade-table";
import { getRecentTrades } from "@/lib/queries";
import { requireTenantServer } from "@/lib/tenant-server";

export const dynamic = "force-dynamic";

export default async function TradesPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  const rawMode = params.mode ?? "paper";
  const mode: "paper" | "testnet" | "mainnet" =
    rawMode === "testnet" || rawMode === "mainnet" ? rawMode : "paper";

  const tenant = await requireTenantServer();

  let trades: Awaited<ReturnType<typeof getRecentTrades>> = [];

  try {
    trades = await getRecentTrades(tenant.id, 100, mode);
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

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Trade History</h1>
      <Card>
        <CardHeader>
          <CardTitle>
            {mode[0].toUpperCase() + mode.slice(1)} Trades ({tradeRows.length})
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <TradeTable trades={tradeRows} />
        </CardContent>
      </Card>
    </div>
  );
}
