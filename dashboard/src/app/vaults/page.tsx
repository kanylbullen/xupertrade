export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Vault = {
  address: string;
  name: string;
  leader_address: string;
  description: string;
  created_at: string | null;
  profit_share_pct: number | null;
  snapshot_at: string | null;
  aum_usd: number | null;
  nav: number | null;
  leader_equity_pct: number | null;
  depositor_count: number | null;
  apr: number | null;
  age_days: number | null;
  roi_7d: number | null;
  roi_30d: number | null;
  roi_90d: number | null;
  roi_180d: number | null;
  roi_365d: number | null;
  max_drawdown_pct: number | null;
  sharpe_180d: number | null;
  qualified: boolean;
  allow_deposits: boolean;
  is_closed: boolean;
};

type MyPosition = {
  vault_address: string;
  vault_name: string | null;
  leader_address: string | null;
  first_seen_at: string | null;
  first_seen_equity_usd: number;
  last_seen_at: string | null;
  last_seen_equity_usd: number;
  pnl_usd: number;
  pnl_pct: number | null;
  locked_until: string | null;
  qualified: boolean;
  failed_filters: string[];
  current_apr: number | null;
  current_sharpe_180d: number | null;
  current_aum_usd: number | null;
  current_max_drawdown_pct: number | null;
  current_leader_equity_pct: number | null;
  snapshot_at: string | null;
};

type MyPositionsResponse = {
  address: string;
  positions: MyPosition[];
  total_equity_usd: number;
};

export default async function VaultsPage({
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

  let vaults: Vault[] = [];
  let myPositions: MyPositionsResponse | null = null;
  let botApiOnline = false;
  try {
    const [listRes, mineRes] = await Promise.all([
      fetch(`${botApiUrl}/api/vaults`, { cache: "no-store" }),
      fetch(`${botApiUrl}/api/vaults/mine`, { cache: "no-store" }),
    ]);
    if (listRes.ok) {
      const data = (await listRes.json()) as { vaults: Vault[] };
      vaults = data.vaults;
      botApiOnline = true;
    }
    if (mineRes.ok) {
      myPositions = (await mineRes.json()) as MyPositionsResponse;
    }
  } catch {
    // bot offline
  }

  return (
    <div className="container mx-auto max-w-6xl p-4 sm:p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">
          Vault scanner
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          HyperLiquid vaults that pass our quality filter (age, AUM, ROI,
          Sharpe, drawdown, manager equity, fee). Mainnet-only — vaults don't
          exist on testnet. Read-only research; no auto-deposit. Polled daily.
        </p>
      </div>

      {!botApiOnline && (
        <Card className="mb-4 border-destructive/50">
          <CardContent className="pt-6 text-sm text-destructive">
            Bot API ({mode}) unreachable — vault data unavailable.
          </CardContent>
        </Card>
      )}

      {myPositions && myPositions.positions.length > 0 && (
        <MyPositionsCard data={myPositions} />
      )}
      {myPositions && myPositions.address && myPositions.positions.length === 0 && (
        <Card className="mb-6">
          <CardContent className="pt-6 text-sm text-muted-foreground">
            Tracking <code className="rounded bg-muted px-1 py-0.5 text-xs">{myPositions.address}</code>{" "}
            — no vault deposits seen yet. Check back after the next daily
            scan, or after your first deposit clears HL&apos;s 1-day lockup.
          </CardContent>
        </Card>
      )}

      {botApiOnline && vaults.length === 0 && (
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            No qualified vaults yet. The scanner runs once a day after the bot
            starts; first run can take a few minutes (catalog is ~14 MB and
            we fetch per-vault details for the candidates). If empty after
            24h, the filter may be too strict — check the breakdown via
            {" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              /api/vaults/&lt;address&gt;
            </code>
            .
          </CardContent>
        </Card>
      )}

      {vaults.length > 0 && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle>Qualified vaults · sorted by Sharpe</CardTitle>
              <Badge variant="outline">{vaults.length} vault{vaults.length === 1 ? "" : "s"}</Badge>
            </div>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase text-muted-foreground">
                  <tr className="border-b">
                    <th className="py-2 pr-3">Vault</th>
                    <th className="py-2 pr-3 text-right">AUM</th>
                    <th className="py-2 pr-3 text-right">APR</th>
                    <th className="py-2 pr-3 text-right">Sharpe (180d)</th>
                    <th className="py-2 pr-3 text-right">Max DD</th>
                    <th className="py-2 pr-3 text-right">ROI 90d</th>
                    <th className="py-2 pr-3 text-right">ROI 180d</th>
                    <th className="py-2 pr-3 text-right">Mgr equity</th>
                    <th className="py-2 pr-3 text-right">Fee</th>
                    <th className="py-2 pr-3 text-right">Age</th>
                  </tr>
                </thead>
                <tbody>
                  {vaults.map((v) => (
                    <tr key={v.address} className="border-b last:border-0">
                      <td className="py-2 pr-3">
                        <div className="font-medium">{v.name || "—"}</div>
                        <div className="font-mono text-xs text-muted-foreground">
                          <a
                            href={`https://app.hyperliquid.xyz/vaults/${v.address}`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="hover:underline"
                          >
                            {v.address.slice(0, 8)}…{v.address.slice(-6)}
                          </a>
                        </div>
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtUsd(v.aum_usd)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtPct(v.apr)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtNum(v.sharpe_180d, 2)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtPct(v.max_drawdown_pct, false)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtPct(v.roi_90d)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtPct(v.roi_180d)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtPct(v.leader_equity_pct, false)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {fmtPct(v.profit_share_pct, false)}
                      </td>
                      <td className="py-2 pr-3 text-right font-mono">
                        {v.age_days === null || v.age_days === undefined
                          ? "—"
                          : `${v.age_days}d`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-4 text-xs text-muted-foreground">
              Quality filter: age ≥ 180d · AUM $200k–$20M · ROI 90/180d &gt; 0%
              · max DD ≤ 25% · Sharpe(180d) &gt; 1.5 · manager equity ≥ 5% ·
              fee ≤ 15%. ROI 365d waived for vaults &lt; 365d old. Vaults
              meeting all rules appear here; failures don't.
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function fmtUsd(v: number | null): string {
  if (v === null || v === undefined) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}k`;
  return `$${v.toFixed(0)}`;
}

function fmtPct(v: number | null, signed = true): string {
  if (v === null || v === undefined) return "—";
  const pct = v * 100;
  if (signed) {
    const sign = pct >= 0 ? "+" : "";
    return `${sign}${pct.toFixed(1)}%`;
  }
  return `${pct.toFixed(1)}%`;
}

function fmtNum(v: number | null, decimals: number): string {
  if (v === null || v === undefined) return "—";
  return v.toFixed(decimals);
}

function MyPositionsCard({ data }: { data: MyPositionsResponse }) {
  return (
    <Card className="mb-6 border-primary/30">
      <CardHeader>
        <div className="flex items-center justify-between gap-4">
          <div>
            <CardTitle>My vault positions</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              Tracking{" "}
              <code className="rounded bg-muted px-1 py-0.5">
                {shortAddr(data.address)}
              </code>{" "}
              — refreshed daily by the scanner. P&amp;L is approximate
              (first-seen vs current equity), not a true cost basis.
            </p>
          </div>
          <div className="text-right">
            <div className="text-xs text-muted-foreground">Total equity</div>
            <div className="font-mono text-lg font-semibold">
              ${data.total_equity_usd.toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {data.positions.map((p) => (
            <PositionRow key={p.vault_address} p={p} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function PositionRow({ p }: { p: MyPosition }) {
  const pnlSign = p.pnl_usd >= 0 ? "+" : "";
  const pnlColor = p.pnl_usd >= 0 ? "text-green-500" : "text-red-500";
  const lockMs = p.locked_until ? new Date(p.locked_until).getTime() : 0;
  const locked = lockMs > Date.now();
  const stillQualifies = p.qualified;
  const verdict = !p.snapshot_at
    ? { tone: "outline" as const, text: "no scoring yet" }
    : stillQualifies
      ? { tone: "default" as const, text: "still qualifies" }
      : { tone: "destructive" as const, text: "no longer qualifies" };

  return (
    <div className="rounded-lg border p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <a
              href={`https://app.hyperliquid.xyz/vaults/${p.vault_address}`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium hover:underline"
            >
              {p.vault_name || shortAddr(p.vault_address)}
            </a>
            <Badge variant={verdict.tone}>{verdict.text}</Badge>
            {locked && (
              <Badge variant="outline" title={p.locked_until ?? ""}>
                locked
              </Badge>
            )}
          </div>
          <div className="mt-1 font-mono text-xs text-muted-foreground">
            {shortAddr(p.vault_address)}
          </div>
        </div>
        <div className="text-right">
          <div className="font-mono text-base font-semibold">
            ${p.last_seen_equity_usd.toLocaleString(undefined, {
              maximumFractionDigits: 2,
            })}
          </div>
          <div className={`font-mono text-xs ${pnlColor}`}>
            {pnlSign}${p.pnl_usd.toFixed(2)}{" "}
            {p.pnl_pct !== null && (
              <>({pnlSign}{(p.pnl_pct * 100).toFixed(1)}%)</>
            )}
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <Metric label="APR" value={fmtPct(p.current_apr)} />
        <Metric label="Sharpe (180d)" value={fmtNum(p.current_sharpe_180d, 2)} />
        <Metric label="Max DD" value={fmtPct(p.current_max_drawdown_pct, false)} />
        <Metric label="Mgr equity" value={fmtPct(p.current_leader_equity_pct, false)} />
      </div>
      {!stillQualifies && p.failed_filters.length > 0 && (
        <div className="mt-3 rounded bg-destructive/10 p-2 text-xs text-destructive">
          Failed filters:{" "}
          <span className="font-mono">{p.failed_filters.join(", ")}</span>{" "}
          — consider exiting after lockup expires.
        </div>
      )}
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

function shortAddr(a: string): string {
  if (!a) return "";
  return `${a.slice(0, 8)}…${a.slice(-6)}`;
}
