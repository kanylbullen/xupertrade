export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Coin = {
  identifier: string;
  symbol: string;
  name: string;
  icon: string;
  rank: number | null;
  count: number;
  price_usd: number;
  value_usd: number;
  price_change_24h_pct: number | null;
  price_change_7d_pct: number | null;
  pnl_24h_usd: number | null;
  pnl_all_time_usd: number | null;
  pnl_unrealized_usd: number | null;
  pnl_realized_usd: number | null;
  avg_buy_usd: number | null;
  avg_sell_usd: number | null;
  risk_score: number | null;
  liquidity_score: number | null;
  volatility_score: number | null;
};

type ProviderResult = {
  name: string;
  ok: boolean;
  error: string;
  coins: Coin[];
  total_value_usd: number;
  total_pnl_24h_usd: number;
  total_pnl_all_time_usd: number;
  fetched_at: string;
  cached: boolean;
};

type PortfolioResponse = {
  configured: boolean;
  providers: ProviderResult[];
};

const PROVIDER_BLURB: Record<string, string> = {
  rotki: "Crypto + DeFi (self-hosted Rotki)",
  ghostfolio: "All asset classes (self-hosted Ghostfolio)",
  coinstats: "CEX + wallets (CoinStats SaaS)",
};

export default async function PortfolioPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string; fresh?: string }>;
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

  const apiKey = process.env.API_KEY || "";
  const headers: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};

  const upstreamUrl = `${botApiUrl}/api/portfolio/coins${
    params.fresh === "1" ? "?fresh=1" : ""
  }`;

  let data: PortfolioResponse | null = null;
  let networkError = false;
  let httpStatus: number | null = null;
  try {
    const res = await fetch(upstreamUrl, { cache: "no-store", headers });
    httpStatus = res.status;
    if (res.ok) {
      data = (await res.json()) as PortfolioResponse;
    }
  } catch {
    networkError = true;
  }
  const botApiOnline = !networkError && httpStatus !== null;

  const grandTotal = (data?.providers ?? [])
    .filter((p) => p.ok)
    .reduce((s, p) => s + p.total_value_usd, 0);

  return (
    <div className="container mx-auto max-w-6xl p-4 sm:p-6">
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">
            Portfolio
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Read-only view across every configured provider. Each shows up
            as its own section. Refreshed live with a 5-minute Redis cache
            per provider; add{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">?fresh=1</code>
            {" "}to bust.
          </p>
        </div>
        {data?.configured && data.providers.length > 0 && (
          <div className="text-right">
            <div className="text-xs text-muted-foreground">Grand total</div>
            <div className="font-mono text-2xl font-semibold">
              ${grandTotal.toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })}
            </div>
          </div>
        )}
      </div>

      {networkError && (
        <Card className="mb-4 border-destructive/50">
          <CardContent className="pt-6 text-sm text-destructive">
            Bot API ({mode}) unreachable — network error.
          </CardContent>
        </Card>
      )}
      {botApiOnline && httpStatus !== null && httpStatus >= 400 && (
        <Card className="mb-4 border-destructive/50">
          <CardContent className="pt-6 text-sm text-destructive">
            Bot API responded HTTP {httpStatus}.{" "}
            {httpStatus === 401 && (
              <>
                Auth required — set{" "}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">
                  API_KEY
                </code>{" "}
                in the dashboard env so it can forward{" "}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">
                  X-Api-Key
                </code>
                .
              </>
            )}
          </CardContent>
        </Card>
      )}

      {botApiOnline && data && !data.configured && <NotConfiguredCard />}

      {botApiOnline && data && data.configured &&
        data.providers.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            All listed providers had missing credentials. Check the env
            vars on the bot-testnet container.
          </CardContent>
        </Card>
      )}

      {data?.providers.map((p) => (
        <ProviderSection key={p.name} provider={p} />
      ))}
    </div>
  );
}

function NotConfiguredCard() {
  return (
    <Card>
      <CardContent className="pt-6 text-sm text-muted-foreground space-y-3">
        <p>
          No portfolio providers configured. Set{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-xs">
            PORTFOLIO_PROVIDERS
          </code>{" "}
          in <code className="rounded bg-muted px-1 py-0.5 text-xs">.env</code>{" "}
          on the deploy host to a comma-separated list of:
        </p>
        <ul className="ml-4 list-disc space-y-1">
          <li>
            <code className="rounded bg-muted px-1 py-0.5 text-xs">rotki</code>
            {" "}— self-hosted, free, crypto + DeFi deep
            ({" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              ROTKI_URL
            </code>
            ,{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              ROTKI_USERNAME
            </code>
            ,{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              ROTKI_PASSWORD
            </code>
            )
          </li>
          <li>
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              ghostfolio
            </code>
            {" "}— self-hosted, free, broader asset universe
            ({" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              GHOSTFOLIO_URL
            </code>
            ,{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              GHOSTFOLIO_TOKEN
            </code>
            )
          </li>
          <li>
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              coinstats
            </code>
            {" "}— SaaS, requires Degen plan
            ({" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              COINSTATS_API_KEY
            </code>
            ,{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              COINSTATS_SHARE_TOKEN
            </code>
            )
          </li>
        </ul>
        <p>
          Special value{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-xs">*</code>{" "}
          enables every provider whose creds are set.
        </p>
      </CardContent>
    </Card>
  );
}

function ProviderSection({ provider }: { provider: ProviderResult }) {
  const fetched = provider.fetched_at
    ? new Date(provider.fetched_at).toLocaleTimeString()
    : "—";
  return (
    <Card className="mb-6 border-primary/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <CardTitle className="capitalize">{provider.name}</CardTitle>
              {provider.cached && <Badge variant="outline">cached</Badge>}
              {!provider.ok && (
                <Badge variant="destructive">error</Badge>
              )}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {PROVIDER_BLURB[provider.name] ?? "Portfolio provider"} ·{" "}
              {provider.coins.length} position
              {provider.coins.length === 1 ? "" : "s"} · fetched {fetched}
            </p>
          </div>
          <div className="text-right">
            <div className="text-xs text-muted-foreground">Total value</div>
            <div className="font-mono text-xl font-semibold">
              ${provider.total_value_usd.toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })}
            </div>
            {provider.total_pnl_all_time_usd !== 0 && (
              <div className="mt-1 text-xs">
                <PnlBlob label="all-time" value={provider.total_pnl_all_time_usd} />
              </div>
            )}
            {provider.total_pnl_24h_usd !== 0 && (
              <div className="text-xs">
                <PnlBlob label="24h" value={provider.total_pnl_24h_usd} />
              </div>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {!provider.ok && (
          <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-xs text-foreground">
            <span className="text-muted-foreground">Provider error: </span>
            <span className="font-mono">{provider.error || "unknown"}</span>
          </div>
        )}
        {provider.ok && provider.coins.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No holdings reported. Check the provider has accounts linked.
          </p>
        )}
        {provider.ok && provider.coins.length > 0 && (
          <div className="grid gap-3 md:grid-cols-2">
            {provider.coins.map((c) => (
              <CoinCard key={`${provider.name}:${c.identifier || c.symbol}`} c={c} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PnlBlob({ label, value }: { label: string; value: number }) {
  const sign = value >= 0 ? "+" : "";
  const cls = value >= 0 ? "text-green-500" : "text-red-500";
  return (
    <span className={`font-mono ${cls}`}>
      {sign}${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}{" "}
      <span className="text-muted-foreground">{label}</span>
    </span>
  );
}

function CoinCard({ c }: { c: Coin }) {
  const change24h = c.price_change_24h_pct;
  const change24Color =
    change24h === null
      ? "text-muted-foreground"
      : change24h >= 0
        ? "text-green-500"
        : "text-red-500";
  return (
    <div className="rounded-lg border p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {c.icon && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={c.icon}
              alt=""
              width={24}
              height={24}
              className="h-6 w-6 rounded-full"
            />
          )}
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-semibold">{c.symbol || c.name}</span>
              {c.rank !== null && c.rank <= 100 && (
                <span className="text-xs text-muted-foreground">
                  #{c.rank}
                </span>
              )}
            </div>
            <div className="truncate text-xs text-muted-foreground">
              {c.name}
            </div>
          </div>
        </div>
        <div className="text-right">
          <div className="font-mono text-base font-semibold">
            ${c.value_usd.toLocaleString(undefined, {
              maximumFractionDigits: 2,
            })}
          </div>
          <div className={`font-mono text-xs ${change24Color}`}>
            {change24h === null
              ? "—"
              : `${change24h >= 0 ? "+" : ""}${(change24h * 100).toFixed(2)}% 24h`}
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <Metric
          label="Holdings"
          value={`${c.count.toLocaleString(undefined, {
            maximumFractionDigits: 6,
          })} ${c.symbol}`}
        />
        <Metric
          label="Price"
          value={`$${c.price_usd.toLocaleString(undefined, {
            maximumFractionDigits: c.price_usd < 1 ? 6 : 2,
          })}`}
        />
        <PnlMetric label="24h P&L" value={c.pnl_24h_usd} />
        <PnlMetric label="All-time P&L" value={c.pnl_all_time_usd} />
        {c.avg_buy_usd !== null && (
          <Metric
            label="Avg buy"
            value={`$${c.avg_buy_usd.toLocaleString(undefined, {
              maximumFractionDigits: 2,
            })}`}
          />
        )}
        {c.risk_score !== null && (
          <Metric label="Risk score" value={c.risk_score.toFixed(1)} />
        )}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-muted-foreground">{label}</div>
      <div className="font-mono font-semibold">{value}</div>
    </div>
  );
}

function PnlMetric({ label, value }: { label: string; value: number | null }) {
  if (value === null) {
    return <Metric label={label} value="—" />;
  }
  const sign = value >= 0 ? "+" : "";
  const cls = value >= 0 ? "text-green-500" : "text-red-500";
  return (
    <div>
      <div className="text-muted-foreground">{label}</div>
      <div className={`font-mono font-semibold ${cls}`}>
        {sign}${value.toLocaleString(undefined, {
          maximumFractionDigits: 2,
        })}
      </div>
    </div>
  );
}
