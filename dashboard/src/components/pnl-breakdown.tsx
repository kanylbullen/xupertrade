import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { StrategyPnl, DailyPnl } from "@/lib/queries";

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
                    ${r.fees.toFixed(2)}
                  </TableCell>
                  <TableCell
                    className={`text-right font-mono text-sm font-semibold ${
                      r.realizedPnl >= 0 ? "text-green-500" : "text-red-500"
                    }`}
                  >
                    {r.realizedPnl >= 0 ? "+" : ""}${r.realizedPnl.toFixed(2)}
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

export function DailyPnlTable({ rows }: { rows: DailyPnl[] }) {
  if (rows.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Daily P&L</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">No trades in the last 30 days.</p>
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
        <CardTitle>Daily P&L (last {rows.length} day{rows.length === 1 ? "" : "s"})</CardTitle>
        <p className="text-xs text-muted-foreground">Net = realized + funding</p>
      </CardHeader>
      <CardContent className="space-y-1">
        {sorted.map((r) => {
          const pct = (Math.abs(r.net) / max) * 100;
          const fundingTip = r.funding !== 0
            ? ` (incl. ${r.funding >= 0 ? "+" : ""}$${r.funding.toFixed(2)} funding)`
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
                    r.net >= 0 ? "left-1/2 bg-green-500/40" : "right-1/2 bg-red-500/40"
                  }`}
                  style={{ width: `${pct / 2}%` }}
                />
                <div className="absolute top-0 bottom-0 left-1/2 w-px bg-foreground/20" />
              </div>
              <span
                className={`font-mono text-xs w-24 text-right shrink-0 ${
                  r.net >= 0 ? "text-green-500" : "text-red-500"
                }`}
                title={fundingTip}
              >
                {r.net >= 0 ? "+" : ""}${r.net.toFixed(2)}
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
        <Row label="Fees paid" value={-Math.abs(fees)} muted />
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
  const sign = value >= 0 ? "+" : "";
  const color =
    muted
      ? "text-muted-foreground"
      : value >= 0
      ? "text-green-500"
      : "text-red-500";
  return (
    <div className="flex items-center justify-between border-b border-border/40 pb-2 last:border-0">
      <span className={`text-sm ${muted ? "text-muted-foreground" : ""}`}>
        {label}
      </span>
      <span className={`font-mono ${bold ? "text-base font-semibold" : "text-sm"} ${color}`}>
        {sign}${Math.abs(value).toFixed(2)}
      </span>
    </div>
  );
}
