import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { StrategyPnl, DailyPnl } from "@/lib/queries";
import { formatSignedUsd, moneySign } from "@/lib/money";

// Every amount below goes through formatSignedUsd, so a loss always
// shows its minus. Fees are shown as what they did to P&L: paid fees
// are negative, a net maker rebate positive.

export function StrategyPnlTable({ rows }: { rows: StrategyPnl[] }) {
  if (rows.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Per-strategy P&L</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">No closed trades yet.</p>
        </CardContent>
      </Card>
    );
  }
  const sorted = [...rows].sort((a, b) => b.realizedPnl - a.realizedPnl);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Per-strategy P&L</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Strategy</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">W/L</TableHead>
              <TableHead className="text-right">Win rate</TableHead>
              <TableHead className="text-right">Fees</TableHead>
              <TableHead className="text-right">Realized P&L</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.map((r) => {
              const decisive = r.wins + r.losses;
              const winRate = decisive > 0 ? (r.wins / decisive) * 100 : null;
              return (
                <TableRow key={r.strategyName}>
                  <TableCell className="font-mono text-xs">{r.strategyName}</TableCell>
                  <TableCell className="text-right font-mono text-xs">{r.trades}</TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    <span className="text-green-400">{r.wins}</span>
                    <span className="text-muted-foreground">/</span>
                    <span className="text-red-400">{r.losses}</span>
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {winRate !== null ? `${winRate.toFixed(0)}%` : "—"}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-muted-foreground">
                    {formatSignedUsd(-r.fees)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono text-sm font-semibold ${
                      moneySign(r.realizedPnl) >= 0 ? "text-green-500" : "text-red-500"
                    }`}
                  >
                    {formatSignedUsd(r.realizedPnl)}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function DailyPnlTable({
  rows,
  windowDays,
}: {
  rows: DailyPnl[];
  windowDays: number;
}) {
  if (rows.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Daily P&L (UTC)</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No trades or funding in the last {windowDays} days.
          </p>
        </CardContent>
      </Card>
    );
  }
  // Display newest first
  const sorted = [...rows].sort((a, b) => b.date.localeCompare(a.date));
  const max = Math.max(...sorted.map((r) => Math.abs(r.net)), 1);
  return (
    <Card>
      <CardHeader>
        {/* Rows are only the days with activity, so their count is not
            the window: "last 3 days" used to mean "3 active days". */}
        <CardTitle>Daily P&L (UTC)</CardTitle>
        <p className="text-xs text-muted-foreground">
          {rows.length} day{rows.length === 1 ? "" : "s"} with activity in the
          last {windowDays} days. Net = realized + funding
        </p>
      </CardHeader>
      <CardContent className="space-y-1">
        {sorted.map((r) => {
          const pct = (Math.abs(r.net) / max) * 100;
          const fundingTip = r.funding !== 0
            ? ` (incl. ${formatSignedUsd(r.funding)} funding)`
            : "";
          return (
            <div key={r.date} className="flex items-center gap-3 text-sm">
              <span className="font-mono text-xs text-muted-foreground w-24 shrink-0">
                {r.date}
              </span>
              <span className="text-xs text-muted-foreground w-12 shrink-0">
                {r.trades}t
              </span>
              <div className="flex-1 h-5 relative bg-muted/30 rounded overflow-hidden">
                <div
                  className={`absolute top-0 bottom-0 ${
                    moneySign(r.net) >= 0 ? "left-1/2 bg-green-500/40" : "right-1/2 bg-red-500/40"
                  }`}
                  style={{ width: `${pct / 2}%` }}
                />
                <div className="absolute top-0 bottom-0 left-1/2 w-px bg-foreground/20" />
              </div>
              <span
                className={`font-mono text-xs w-24 text-right shrink-0 ${
                  moneySign(r.net) >= 0 ? "text-green-500" : "text-red-500"
                }`}
                title={fundingTip}
              >
                {formatSignedUsd(r.net)}
              </span>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

export function PnlSummary({
  realized,
  fees,
  funding,
  unrealized,
}: {
  realized: number;
  fees: number;
  funding: number;
  unrealized: number;
}) {
  // Net = realized + funding (fees already in realized via trade.pnl)
  const net = realized + funding;
  const totalWithUnrealized = net + unrealized;
  return (
    <Card>
      <CardHeader>
        <CardTitle>P&L breakdown (all-time)</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-2">
        <Row label="Realized (after fees)" value={realized} />
        <Row label="Unrealized (open positions)" value={unrealized} muted />
        <Row label="Funding (cumulative)" value={funding} />
        <Row label="Fees (in realized)" value={-fees} muted />
        <Row label="Net P&L" value={net} bold />
        <Row label="Total inc. unrealized" value={totalWithUnrealized} bold />
      </CardContent>
    </Card>
  );
}

function Row({
  label,
  value,
  bold = false,
  muted = false,
}: {
  label: string;
  value: number;
  bold?: boolean;
  muted?: boolean;
}) {
  const color =
    muted
      ? "text-muted-foreground"
      : moneySign(value) >= 0
      ? "text-green-500"
      : "text-red-500";
  return (
    <div className="flex items-center justify-between border-b border-border/40 pb-2 last:border-0">
      <span className={`text-sm ${muted ? "text-muted-foreground" : ""}`}>
        {label}
      </span>
      <span className={`font-mono ${bold ? "text-base font-semibold" : "text-sm"} ${color}`}>
        {formatSignedUsd(value)}
      </span>
    </div>
  );
}
