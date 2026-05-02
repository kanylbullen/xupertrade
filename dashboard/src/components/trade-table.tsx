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

type Trade = {
  id: number;
  strategyName: string;
  symbol: string;
  side: string;
  size: number;
  price: number;
  fee: number | null;
  pnl: number | null;
  reason: string | null;
  timestamp: Date | null;
};

export function TradeTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) {
    return (
      <div className="py-8 text-center text-muted-foreground">
        No trades yet.
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Time</TableHead>
          <TableHead>Strategy</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>Side</TableHead>
          <TableHead className="text-right">Size</TableHead>
          <TableHead className="text-right">Price</TableHead>
          <TableHead className="text-right">Fee</TableHead>
          <TableHead>Reason</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {trades.map((trade) => (
          <TableRow key={trade.id}>
            <TableCell className="text-xs text-muted-foreground">
              {trade.timestamp
                ? formatDateTime(trade.timestamp)
                : "-"}
            </TableCell>
            <TableCell>
              <Badge variant="outline">{trade.strategyName}</Badge>
            </TableCell>
            <TableCell className="font-mono">{trade.symbol}</TableCell>
            <TableCell>
              <Badge
                variant={trade.side === "buy" ? "default" : "secondary"}
              >
                {trade.side.toUpperCase()}
              </Badge>
            </TableCell>
            <TableCell className="text-right font-mono">
              {trade.size.toFixed(6)}
            </TableCell>
            <TableCell className="text-right font-mono">
              ${trade.price.toLocaleString()}
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              ${(trade.fee ?? 0).toFixed(2)}
            </TableCell>
            <TableCell className="max-w-[200px] truncate text-xs text-muted-foreground">
              {trade.reason}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
