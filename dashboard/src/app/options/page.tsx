export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { StrategyToggle } from "@/components/strategy-toggle";
import { LeverageInput } from "@/components/leverage-input";
import { MultiCoinToggle } from "@/components/multi-coin-toggle";
import { AuthConfig } from "@/components/auth-config";
import { TlsConfig } from "@/components/tls-config";

export default async function OptionsPage({
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

  type StrategyMeta = { name: string; symbol: string; timeframe: string };
  let strategies: StrategyMeta[] = [];
  let botApiOnline = false;
  try {
    const res = await fetch(`${botApiUrl}/strategies`, { cache: "no-store" });
    if (res.ok) {
      const data = await res.json() as { strategies: StrategyMeta[] };
      strategies = data.strategies;
      botApiOnline = true;
    }
  } catch {
    // bot offline
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Options</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Trading rules, strategy controls, and leverage settings.
        </p>
      </div>

      {/* Trading rules */}
      <Card>
        <CardHeader>
          <CardTitle>Trading Rules</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-start justify-between gap-4 rounded-lg border p-4">
            <div className="space-y-1">
              <p className="font-medium">Allow multiple strategies per coin</p>
              <p className="text-sm text-muted-foreground max-w-md">
                When off, only one strategy can hold a position on a given coin at
                a time. A second strategy won&apos;t open until the first closes.
                Prevents compounding exposure and conflicting long/short positions.
              </p>
            </div>
            <MultiCoinToggle />
          </div>
        </CardContent>
      </Card>

      <Separator />

      {/* Authentication */}
      <AuthConfig />

      <Separator />

      {/* HTTPS / TLS */}
      <TlsConfig />

      <Separator />

      {/* Strategy controls */}
      <Card>
        <CardHeader>
          <CardTitle>Strategies</CardTitle>
        </CardHeader>
        <CardContent>
          {!botApiOnline && (
            <p className="text-sm text-muted-foreground mb-3">
              Bot API unavailable — strategy list could not be loaded.
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            {strategies.map((s) => (
              <div
                key={s.name}
                className="flex items-center justify-between rounded-lg border p-3"
              >
                <div>
                  <p className="font-semibold text-sm">{s.name}</p>
                  <p className="text-xs text-muted-foreground font-mono">
                    {s.symbol} {s.timeframe}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <LeverageInput name={s.name} />
                  <StrategyToggle name={s.name} />
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
