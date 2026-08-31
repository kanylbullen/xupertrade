import { formatDateTime } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/**
 * One row per saved CLI backtest run. Mirrors `TradeTable` styling.
 *
 * Unit note: `totalReturnPct`, `apr`, `winRate`, `maxDrawdownPct` and
 * `feeRate` arrive as FRACTIONS from the DB (the bot's CLI multiplies
 * by 100 only when printing) — hence the ×100 here. `slippageBps` is
 * already in bps; `sharpe` is a plain ratio.
 */
type BacktestRun = {
  id: number;
  strategyName: string;
  symbol: string;
  timeframe: string;
  leverage: number;
  days: number;
  totalReturnPct: number;
  apr: number;
  sharpe: number;
  maxDrawdownPct: number;
  numTrades: number;
  numRoundTrips: number;
  winRate: number;
  feesPaid: number;
  positionSizeUsd: number;
  feeRate: number;
  slippageBps: number;
  createdAt: Date | null;
};

const pct = (v: number, digits = 1) =>
  `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;

export function BacktestRunsTable({ runs }: { runs: BacktestRun[] }) {
  if (runs.length === 0) {
    return (
      <div className="py-8 text-center text-muted-foreground">
        No backtest runs yet. Runs are saved by the bot CLI:
        <code className="ml-1 rounded bg-muted px-1 py-0.5 text-xs">
          python -m hypertrade.backtest --strategy &lt;name&gt; --days N
        </code>
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Run at</TableHead>
          <TableHead>Strategy</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>TF</TableHead>
          <TableHead className="text-right">Lev</TableHead>
          <TableHead className="text-right">Days</TableHead>
          <TableHead className="text-right">Return</TableHead>
          <TableHead className="text-right">APR</TableHead>
          <TableHead className="text-right">Sharpe</TableHead>
          <TableHead className="text-right">Max DD</TableHead>
          <TableHead className="text-right">Win rate</TableHead>
          <TableHead className="text-right">Trades</TableHead>
          <TableHead className="text-right">Size</TableHead>
          <TableHead className="text-right">Fee</TableHead>
          <TableHead className="text-right">Slip</TableHead>
          <TableHead className="text-right">Fees paid</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => (
          <TableRow key={run.id}>
            <TableCell className="text-xs text-muted-foreground">
              {run.createdAt ? formatDateTime(run.createdAt) : "-"}
            </TableCell>
            <TableCell>
              <Badge variant="outline">{run.strategyName}</Badge>
            </TableCell>
            <TableCell className="font-mono">{run.symbol}</TableCell>
            <TableCell className="text-muted-foreground">{run.timeframe}</TableCell>
            <TableCell className="text-right font-mono">{run.leverage}x</TableCell>
            <TableCell className="text-right font-mono">{run.days.toFixed(0)}</TableCell>
            <TableCell className="text-right font-mono">
              {pct(run.totalReturnPct)}
            </TableCell>
            <TableCell className="text-right font-mono">{pct(run.apr)}</TableCell>
            <TableCell className="text-right font-mono">
              {run.sharpe.toFixed(2)}
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              {(run.maxDrawdownPct * 100).toFixed(1)}%
            </TableCell>
            <TableCell className="text-right font-mono">
              {(run.winRate * 100).toFixed(1)}%
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              {run.numTrades} ({run.numRoundTrips})
            </TableCell>
            <TableCell className="text-right font-mono">
              ${run.positionSizeUsd.toLocaleString()}
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              {(run.feeRate * 100).toFixed(3)}%
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              {run.slippageBps.toFixed(0)}bps
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              ${run.feesPaid.toFixed(2)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
