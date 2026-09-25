import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/stat-card";
import { formatDateTime } from "@/lib/format";
import {
  formatSignedPct,
  formatSignedUsd,
  formatUsd,
  moneySign,
} from "@/lib/money";
import {
  SNAPSHOT_SLACK_MS,
  type EquityChange,
  type EquityPoint,
} from "@/lib/overview-numbers";
import type { DailyPnl } from "@/lib/queries";

export type OverviewStatsProps = {
  modeLabel: string;
  dbConnected: boolean;
  latestEquity: EquityPoint | null;
  /** One per fixed window, shortest first (lib/overview-numbers.ts). */
  equityChanges: EquityChange[];
  realized: { realizedPnl: number; fees: number; trades: number };
  /** Today's UTC row from getDailyPnl, null when there was nothing today. */
  today: DailyPnl | null;
  nowMs: number;
};

const NO_SNAPSHOTS = "No equity snapshots yet";

function trendOf(value: number): "up" | "down" | "neutral" {
  const sign = moneySign(value);
  return sign > 0 ? "up" : sign < 0 ? "down" : "neutral";
}

/**
 * The overview's four headline cards. Pure: the page does the reads and
 * passes plain data, so the empty and negative cases can be rendered in
 * a test.
 *
 * Nothing here invents a number. With the database unreachable or no
 * equity snapshot written yet, a card says so instead of showing the
 * $10,000 placeholder (and a $0.00 change against it) it used to.
 */
export function OverviewStats({
  modeLabel,
  dbConnected,
  latestEquity,
  equityChanges,
  realized,
  today,
  nowMs,
}: OverviewStatsProps) {
  const equityMissing = !dbConnected ? "DB offline" : latestEquity ? null : NO_SNAPSHOTS;
  // A stopped bot writes no snapshots; say how old the figure is.
  const equitySubtitle =
    equityMissing ??
    (latestEquity && nowMs - latestEquity.at.getTime() > SNAPSHOT_SLACK_MS
      ? `${modeLabel} · last snapshot ${formatDateTime(latestEquity.at)}`
      : modeLabel);

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        title="Total Equity"
        value={latestEquity && !equityMissing ? formatUsd(latestEquity.equity) : "—"}
        subtitle={equitySubtitle}
        trend="neutral"
      />
      <EquityChangeCard changes={equityChanges} missing={equityMissing} />
      <StatCard
        title="Realized P&L (all-time)"
        value={dbConnected ? formatSignedUsd(realized.realizedPnl) : "—"}
        subtitle={
          dbConnected
            ? `${realized.trades} trades · fees ${formatSignedUsd(-realized.fees)}`
            : "DB offline"
        }
        trend={dbConnected ? trendOf(realized.realizedPnl) : "neutral"}
      />
      <StatCard
        title="Today's P&L (UTC)"
        value={dbConnected ? formatSignedUsd(today?.net ?? 0) : "—"}
        subtitle={
          !dbConnected
            ? "DB offline"
            : today
              ? `Realized ${formatSignedUsd(today.realizedPnl)} · funding ${formatSignedUsd(today.funding)} · ${today.trades} trades`
              : "No trades or funding yet today"
        }
        trend={dbConnected ? trendOf(today?.net ?? 0) : "neutral"}
      />
    </div>
  );
}

function EquityChangeCard({
  changes,
  missing,
}: {
  changes: EquityChange[];
  missing: string | null;
}) {
  // Windows that fall back to the same partial baseline ("since …")
  // would repeat one row; show it once.
  const rows = changes.filter(
    (c, i) =>
      !(
        i > 0 &&
        c.since !== null &&
        changes[i - 1].since?.getTime() === c.since.getTime()
      ),
  );
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm font-medium">Equity change</CardTitle>
      </CardHeader>
      <CardContent>
        {missing ? (
          <>
            <div className="text-2xl font-bold text-muted-foreground">—</div>
            <p className="text-xs text-muted-foreground">{missing}</p>
          </>
        ) : (
          <>
            <dl className="space-y-1">
              {rows.map((c) => (
                <div
                  key={c.label}
                  className="flex items-baseline justify-between gap-2 text-sm"
                >
                  <dt className="text-muted-foreground">
                    {c.since ? `since ${formatDateTime(c.since)}` : c.label}
                  </dt>
                  <dd
                    className={`font-mono ${
                      c.change === null
                        ? "text-muted-foreground"
                        : trendOf(c.change) === "up"
                          ? "text-green-500"
                          : trendOf(c.change) === "down"
                            ? "text-red-500"
                            : ""
                    }`}
                  >
                    {c.change === null
                      ? "no snapshot"
                      : c.pct === null
                        ? formatSignedUsd(c.change)
                        : `${formatSignedUsd(c.change)} (${formatSignedPct(c.pct)})`}
                  </dd>
                </div>
              ))}
            </dl>
            <p className="mt-1 text-xs text-muted-foreground">
              Includes deposits and withdrawals
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
