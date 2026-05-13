export const revalidate = 60;

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { requireTenantServer } from "@/lib/tenant-server";
import { db, tenantBots } from "@/lib/db";
import { getBotApiUrl } from "@/lib/bot-api";
import { and, eq } from "drizzle-orm";

type Check = {
  name: string;
  passed: boolean;
  value: string;
  threshold: string;
  weight: number;
};

type SignalState = {
  name: string;
  asset: string;
  description: string;
  triggered: boolean;
  score: number;
  threshold: number;
  verdict: string;
  checks: Check[];
  notes: string;
  error: string | null;
};

type ManualLevels = {
  id: number;
  recorded_at: string | null;
  sth_cost_basis_usd: number | null;
  lth_cost_basis_usd: number | null;
  realized_price_usd: number | null;
  cvdd_usd: number | null;
  source: string | null;
  notes: string | null;
};

type Purchase = {
  id: number;
  purchased_at: string | null;
  asset: string;
  exchange: string | null;
  amount_local: number;
  local_currency: string;
  btc_amount: number;
  btc_price_usd: number;
  btc_price_local: number | null;
  fx_rate: number | null;
  zone: string | null;
  cold_storage_at: string | null;
  cold_storage_address: string | null;
  notes: string | null;
};

export default async function HodlPage() {
  // HODL signals are mainnet-only by design (Decision 2 of the
  // sidebar nav refactor — long-term holding signals only make sense
  // against the real-money chain). No mode picker here. The existing
  // "bot offline" empty state renders when no mainnet bot is running.
  const mode = "mainnet" as const;

  const tenant = await requireTenantServer();
  const botRows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenant.id), eq(tenantBots.mode, mode)))
    .limit(1);
  const botApiUrl = botRows[0] ? getBotApiUrl(botRows[0]) : null;

  // /api/hodl/* are auth-gated when API_KEY is set on the bot. Server-side
  // render forwards the dashboard's API_KEY so we don't 401 ourselves.
  const apiKey = process.env.API_KEY || "";
  const authHeaders: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};

  let signals: SignalState[] = [];
  let levels: ManualLevels | null = null;
  let purchases: Purchase[] = [];
  let botApiOnline = false;
  if (botApiUrl) try {
    const fetchOpts = {
      next: { revalidate: 60 },
      headers: authHeaders,
      signal: AbortSignal.timeout(8000),
    } as const;
    const [signalsRes, levelsRes, purchasesRes] = await Promise.all([
      fetch(`${botApiUrl}/api/hodl/signals`, fetchOpts),
      fetch(`${botApiUrl}/api/hodl/levels`, fetchOpts),
      fetch(`${botApiUrl}/api/hodl/purchases?limit=20`, fetchOpts),
    ]);
    if (signalsRes.ok) {
      const data = (await signalsRes.json()) as { signals: SignalState[] };
      signals = data.signals;
      botApiOnline = true;
    }
    if (levelsRes.ok) {
      const data = (await levelsRes.json()) as { latest: ManualLevels | null };
      levels = data.latest;
    }
    if (purchasesRes.ok) {
      const data = (await purchasesRes.json()) as { purchases: Purchase[] };
      purchases = data.purchases;
    }
  } catch {
    // bot offline
  }

  return (
    <div className="container mx-auto max-w-6xl p-4 sm:p-6">
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">
            HODL signals
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Long-term accumulation signals for spot positions. Advisory only —
            these don't trade. They tell you when conditions look favorable to
            add to a position you intend to hold.
          </p>
        </div>
      </div>

      {!botApiOnline && (
        <Card className="mb-4 border-destructive/50">
          <CardContent className="pt-6 text-sm text-destructive">
            Bot API ({mode}) unreachable — signal data unavailable.
          </CardContent>
        </Card>
      )}

      {botApiOnline && signals.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No signals registered yet.
          </CardContent>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {signals.map((s) => (
          <SignalCard key={s.name} signal={s} />
        ))}
      </div>

      <ManualLevelsCard levels={levels} />
      <PurchasesCard purchases={purchases} />
    </div>
  );
}

function ManualLevelsCard({ levels }: { levels: ManualLevels | null }) {
  const ageDays = levels?.recorded_at
    ? Math.floor(
        (Date.now() - new Date(levels.recorded_at).getTime()) / 86_400_000,
      )
    : null;
  const stale = ageDays !== null && ageDays > 14;

  return (
    <Card className="mt-6">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Manual on-chain levels</CardTitle>
          {ageDays !== null && (
            <Badge variant={stale ? "destructive" : "outline"}>
              {ageDays === 0 ? "today" : `${ageDays}d old`}
              {stale && " — stale"}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {!levels ? (
          <p className="text-sm text-muted-foreground">
            No manual levels recorded yet. Run{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              uv run python -m scripts.record_levels --sth 81000 --realized 54300 --cvdd 44200
            </code>{" "}
            after reading the latest Roots newsletter to feed the
            btc_accumulation_zone signal with ground-truth data instead of
            SMA proxies.
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <Metric label="STH cost basis" value={levels.sth_cost_basis_usd} />
            <Metric label="Realized Price" value={levels.realized_price_usd} />
            <Metric label="LTH cost basis" value={levels.lth_cost_basis_usd} />
            <Metric label="CVDD" value={levels.cvdd_usd} />
            {(levels.source || levels.notes) && (
              <p className="col-span-full text-xs text-muted-foreground">
                {levels.source}
                {levels.notes ? ` — ${levels.notes}` : ""}
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: number | null }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-mono text-base font-semibold">
        {value === null || value === undefined ? "—" : `$${value.toLocaleString()}`}
      </div>
    </div>
  );
}

function PurchasesCard({ purchases }: { purchases: Purchase[] }) {
  if (purchases.length === 0) {
    return (
      <Card className="mt-6">
        <CardHeader>
          <CardTitle>Recent HODL purchases</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No HODL purchases logged yet. Use{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              uv run python -m scripts.record_purchase --amount-sek 5000 --btc 0.005 --btc-usd 80100 --zone yellow
            </code>{" "}
            after each spot buy. K4 cost-basis (SEK/BTC at purchase time) is
            stored automatically.
          </p>
        </CardContent>
      </Card>
    );
  }

  const totalBtc = purchases.reduce((s, p) => s + p.btc_amount, 0);
  const totalLocal = purchases.reduce((s, p) => s + p.amount_local, 0);
  const avgCostLocal = totalBtc > 0 ? totalLocal / totalBtc : 0;
  const ccy = purchases[0]?.local_currency ?? "SEK";

  return (
    <Card className="mt-6">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Recent HODL purchases</CardTitle>
          <div className="text-right text-xs text-muted-foreground">
            <div>{totalBtc.toFixed(6)} BTC</div>
            <div>
              {totalLocal.toLocaleString()} {ccy} ·{" "}
              {avgCostLocal.toLocaleString(undefined, {
                maximumFractionDigits: 0,
              })}{" "}
              {ccy}/BTC avg
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-muted-foreground">
              <tr className="border-b">
                <th className="py-2 pr-3">Date</th>
                <th className="py-2 pr-3">BTC</th>
                <th className="py-2 pr-3">Spent</th>
                <th className="py-2 pr-3">Cost basis</th>
                <th className="py-2 pr-3">Zone</th>
                <th className="py-2 pr-3">Cold</th>
              </tr>
            </thead>
            <tbody>
              {purchases.map((p) => {
                const date = p.purchased_at
                  ? new Date(p.purchased_at).toISOString().slice(0, 10)
                  : "—";
                const cold = p.cold_storage_at
                  ? new Date(p.cold_storage_at).toISOString().slice(0, 10)
                  : "—";
                return (
                  <tr key={p.id} className="border-b last:border-0">
                    <td className="py-2 pr-3 font-mono text-xs">{date}</td>
                    <td className="py-2 pr-3 font-mono">
                      {p.btc_amount.toFixed(6)}
                    </td>
                    <td className="py-2 pr-3 font-mono">
                      {p.amount_local.toLocaleString()} {p.local_currency}
                    </td>
                    <td className="py-2 pr-3 font-mono">
                      {p.btc_price_local
                        ? `${p.btc_price_local.toLocaleString(undefined, {
                            maximumFractionDigits: 0,
                          })} ${p.local_currency}/BTC`
                        : `$${p.btc_price_usd.toLocaleString()}`}
                    </td>
                    <td className="py-2 pr-3">
                      {p.zone ? (
                        <Badge variant="outline">{p.zone}</Badge>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{cold}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

function SignalCard({ signal }: { signal: SignalState }) {
  const scorePct = Math.round(signal.score * 100);
  const thresholdPct = Math.round(signal.threshold * 100);
  const verdictTone = signal.error
    ? "destructive"
    : signal.triggered
      ? "default"
      : signal.score >= 0.4
        ? "secondary"
        : "outline";
  const barColor = signal.error
    ? "bg-destructive"
    : signal.triggered
      ? "bg-green-500"
      : signal.score >= 0.4
        ? "bg-yellow-500"
        : "bg-muted-foreground/40";

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2">
              <span>{prettyName(signal.name)}</span>
              <Badge variant="outline">{signal.asset}</Badge>
            </CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              {signal.description}
            </p>
          </div>
          <Badge variant={verdictTone}>{signal.verdict}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Score bar */}
        <div>
          <div className="mb-1 flex items-center justify-between text-xs">
            <span className="text-muted-foreground">Signal score</span>
            <span className="font-mono">
              {scorePct}% / {thresholdPct}% to trigger
            </span>
          </div>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={`h-full ${barColor} transition-all`}
              style={{ width: `${scorePct}%` }}
            />
          </div>
        </div>

        <Separator />

        {/* Checks */}
        <div className="space-y-2">
          {signal.checks.map((c) => (
            <div key={c.name} className="flex items-start gap-3 text-sm">
              <span
                className={`mt-0.5 inline-block h-4 w-4 shrink-0 rounded-full ${
                  c.passed ? "bg-green-500" : "bg-muted-foreground/30"
                }`}
                aria-label={c.passed ? "passed" : "failed"}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{c.name}</span>
                  <span className="font-mono text-xs text-muted-foreground">
                    need {c.threshold}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground">{c.value}</div>
              </div>
            </div>
          ))}
        </div>

        {signal.notes && (
          <p className="border-t pt-3 text-xs italic text-muted-foreground">
            {signal.notes}
          </p>
        )}
        {signal.error && (
          <p className="border-t pt-3 text-xs text-destructive">
            {signal.error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function prettyName(name: string): string {
  return name
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
