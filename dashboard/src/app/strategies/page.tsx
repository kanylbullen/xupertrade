export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { TradingViewChart } from "@/components/tv-chart";
import {
  fetchStrategyCatalog,
  type CatalogStrategy,
} from "@/lib/strategy-catalog";
import { requireTenantServer } from "@/lib/tenant-server";

/**
 * Strategy reference.
 *
 * This page used to carry its own hardcoded array of 21 strategy
 * descriptors — ~800 lines of data living inside a React component,
 * with no link to the modules it described. Symbol and timeframe could
 * (and did) drift from what the bot actually ran.
 *
 * It now renders whatever the tenant's bot reports from `/strategies`:
 * live name/symbol/timeframe from the strategy registry, merged with
 * prose from `bot/hypertrade/strategies/meta/<name>.json` colocated
 * with each module.
 *
 * No APR / Sharpe / drawdown cells: those came from a hand-written
 * `stats` block in the meta files that nothing kept current, and are
 * gone (roadmap NU-6). Measured runs are on /backtests.
 */

function StrategyDetail({ s }: { s: CatalogStrategy }) {
  const hasChart = Boolean(s.symbol && s.timeframe);
  return (
    <Card id={s.name} className="scroll-mt-6">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle className="text-xl font-mono">{s.name}</CardTitle>
          {hasChart && (
            <Badge variant="outline" className="font-mono">
              {s.symbol} {s.timeframe}
            </Badge>
          )}
        </div>
        {s.summary && <p className="text-muted-foreground">{s.summary}</p>}
      </CardHeader>
      <CardContent className="space-y-6">
        {s.logic && s.logic.length > 0 && (
          <>
            <Separator />
            <div>
              <h3 className="mb-2 font-semibold">How it works</h3>
              <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
                {s.logic.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </div>
          </>
        )}

        {(s.strengths?.length || s.weaknesses?.length) && (
          <div className="grid gap-4 sm:grid-cols-2">
            {s.strengths && s.strengths.length > 0 && (
              <div>
                <h3 className="mb-2 font-semibold text-green-400">Strengths</h3>
                <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                  {s.strengths.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {s.weaknesses && s.weaknesses.length > 0 && (
              <div>
                <h3 className="mb-2 font-semibold text-red-400">Weaknesses</h3>
                <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                  {s.weaknesses.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {s.params && Object.keys(s.params).length > 0 && (
          <>
            <Separator />
            <div>
              <h3 className="mb-2 font-semibold">Parameters</h3>
              <div className="flex flex-wrap gap-2">
                {Object.entries(s.params).map(([key, value]) => (
                  <Badge key={key} variant="outline" className="font-mono text-xs">
                    {key}: {String(value)}
                  </Badge>
                ))}
              </div>
            </div>
          </>
        )}

        {hasChart && (
          <>
            <Separator />
            <div>
              <h3 className="mb-2 font-semibold">Live Chart with Indicators</h3>
              <TradingViewChart
                symbol={s.symbol!}
                timeframe={s.timeframe!}
                height={450}
                strategy={s.name}
              />
            </div>
          </>
        )}

        <div className="flex items-center justify-between pt-2">
          {s.tvUrl ? (
            <a
              href={s.tvUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-muted-foreground underline hover:text-foreground"
            >
              View on TradingView ↗
            </a>
          ) : (
            <span />
          )}
          <a
            href="#"
            className="text-xs text-muted-foreground underline hover:text-foreground"
          >
            ↑ Back to top
          </a>
        </div>
      </CardContent>
    </Card>
  );
}

export default async function StrategiesPage() {
  const tenant = await requireTenantServer();
  const strategies = await fetchStrategyCatalog(tenant.id);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Strategies</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Click a strategy below to jump to its details. Leverage and on/off
          controls live on the{" "}
          <a href="/options" className="underline hover:text-foreground">
            options page
          </a>
          ; per-bot runtime status lives on{" "}
          <a href="/settings/bots" className="underline hover:text-foreground">
            settings → bots
          </a>
          .
        </p>
      </div>

      {strategies.length === 0 ? (
        <Card>
          <CardContent className="py-10 text-center">
            <p className="text-sm text-muted-foreground">
              No running bot to read the strategy list from.
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              This page reflects what your bot actually has registered, so it
              needs one running. Start one under{" "}
              <a
                href="/settings/bots"
                className="underline hover:text-foreground"
              >
                settings → bots
              </a>
              .
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {strategies.map((s) => (
              <a
                key={s.name}
                href={`#${s.name}`}
                className="block rounded-lg border bg-card p-4 transition hover:border-foreground/40 hover:bg-accent/50"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="font-mono text-sm font-semibold">{s.name}</div>
                  {s.symbol && s.timeframe && (
                    <Badge
                      variant="outline"
                      className="shrink-0 font-mono text-[10px]"
                    >
                      {s.symbol} {s.timeframe}
                    </Badge>
                  )}
                </div>
                {s.summary && (
                  <p className="mt-2 line-clamp-3 text-xs text-muted-foreground">
                    {s.summary}
                  </p>
                )}
              </a>
            ))}
          </div>

          <Separator />

          {strategies.map((s) => (
            <StrategyDetail key={s.name} s={s} />
          ))}
        </>
      )}
    </div>
  );
}
