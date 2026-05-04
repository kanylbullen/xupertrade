export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";

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

export default async function HodlPage({
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

  let signals: SignalState[] = [];
  let botApiOnline = false;
  try {
    const res = await fetch(`${botApiUrl}/api/hodl/signals`, { cache: "no-store" });
    if (res.ok) {
      const data = (await res.json()) as { signals: SignalState[] };
      signals = data.signals;
      botApiOnline = true;
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
    </div>
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
