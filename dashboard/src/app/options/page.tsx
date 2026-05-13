export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { StrategyToggle } from "@/components/strategy-toggle";
import { LeverageInput } from "@/components/leverage-input";
import { MultiCoinToggle } from "@/components/multi-coin-toggle";
import { OptionsModePicker } from "@/components/options-mode-picker";
import { requireTenantServer } from "@/lib/tenant-server";
import { db, tenantBots } from "@/lib/db";
import { getBotApiUrl } from "@/lib/bot-api";
import { and, eq } from "drizzle-orm";

export default async function OptionsPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  const rawMode = params.mode ?? "paper";
  const mode: "paper" | "testnet" | "mainnet" =
    rawMode === "testnet" || rawMode === "mainnet" ? rawMode : "paper";

  const tenant = await requireTenantServer();
  const botRows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenant.id), eq(tenantBots.mode, mode)))
    .limit(1);
  const botApiUrl = botRows[0] ? getBotApiUrl(botRows[0]) : null;

  // /strategies is auth-gated when API_KEY is set on the bot. Forward the
  // dashboard's API_KEY from server-side env so SSR doesn't 401.
  const apiKey = process.env.API_KEY || "";
  const authHeaders: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};

  type StrategyMeta = { name: string; symbol: string; timeframe: string };
  let strategies: StrategyMeta[] = [];
  let botApiOnline = false;
  if (botApiUrl) try {
    const res = await fetch(`${botApiUrl}/strategies`, { cache: "no-store", headers: authHeaders });
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
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">Options</h1>
            <p className="text-sm text-muted-foreground mt-1">
              Trading rules, strategy controls, and leverage settings.
              <span className="ml-1 text-xs">
                (Per-bot — picker on the right.)
              </span>
            </p>
          </div>
          {/* Mode picker (Copilot review fix on PR #105 — Options
              page is per-bot but the sidebar nav has no global mode
              toggle, so without this the operator silently lands on
              paper-mode controls). Sticky in localStorage. */}
          <OptionsModePicker active={mode} />
        </div>
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
            <MultiCoinToggle mode={mode} />
          </div>
        </CardContent>
      </Card>

      <Separator />

      {/* Authentication + TLS config UI intentionally NOT shown on
          this page. Authentik OIDC is on for every signed-in user
          (tenant-level toggles wouldn't make sense), and TLS is a
          host-level Caddy concern that doesn't belong on a per-
          tenant page either.

          Current state (UI-cleanup only):
          - /api/tls/{config,configure} stay behind requireOperator
            (Phase 6c PR γ #59). Underlying Caddy config still lives
            in Redis where the removed UI wrote it.
          - /api/auth/configure is currently NOT operator-gated —
            tenant-isolation gap left over from PR ε that the
            followup PR will close along with migrating both stores
            from Redis to Phase-injected env vars. */}

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
                  <LeverageInput name={s.name} mode={mode} />
                  <StrategyToggle name={s.name} mode={mode} />
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
