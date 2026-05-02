import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatCard } from "@/components/stat-card";
import { LiveLog } from "@/components/live-log";
import { BotControls } from "@/components/bot-controls";
import { getLatestEquity, getOpenPositions, getRecentTrades } from "@/lib/queries";

export const dynamic = "force-dynamic";

export default async function StatusPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  const rawMode = params.mode ?? "paper";
  const mode: "paper" | "testnet" | "mainnet" =
    rawMode === "testnet" || rawMode === "mainnet" ? rawMode : "paper";

  let equity = 10_000;
  let positionCount = 0;
  let tradeCount = 0;
  let lastTradeTime = "—";
  let dbOnline = false;

  try {
    const [latestEquity, positions, trades] = await Promise.all([
      getLatestEquity(mode),
      getOpenPositions(mode),
      getRecentTrades(1, mode),
    ]);
    equity = latestEquity?.totalEquity ?? 10_000;
    positionCount = positions.length;
    tradeCount = trades.length > 0 ? 1 : 0;
    if (trades.length > 0 && trades[0].timestamp) {
      lastTradeTime = new Date(trades[0].timestamp).toLocaleString("sv-SE", {
        timeZone: "Europe/Stockholm",
        hour12: false,
      });
    }
    dbOnline = true;
  } catch {
    // DB offline
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-bold">Bot Status</h1>
        <Badge
          variant="outline"
          className={
            dbOnline
              ? "border-green-500 text-green-400"
              : "border-red-500 text-red-400"
          }
        >
          {dbOnline ? "Online" : "Offline"}
        </Badge>
      </div>

      <BotControls />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="Equity"
          value={`$${equity.toLocaleString()}`}
          subtitle={mode === "paper" ? "Paper" : mode === "testnet" ? "Testnet" : "Mainnet"}
          trend="up"
        />
        <StatCard
          title="Open Positions"
          value={String(positionCount)}
          subtitle="Active"
        />
        <StatCard
          title="Mode"
          value={mode[0].toUpperCase() + mode.slice(1)}
          subtitle="Exchange mode"
        />
        <StatCard
          title="Last Trade"
          value={tradeCount > 0 ? lastTradeTime : "—"}
          subtitle="Most recent"
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Live Event Log</CardTitle>
        </CardHeader>
        <CardContent>
          <LiveLog />
        </CardContent>
      </Card>
    </div>
  );
}
