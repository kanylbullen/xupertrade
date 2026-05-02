import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";

export type PositionRow = {
  symbol: string;
  side: string;
  size: number;
  entryPrice: number;
  unrealizedPnl: number;
  liquidationPrice?: number | null;
  strategies: string[];
  source: "exchange" | "db";
};

const TV_SYMBOL_MAP: Record<string, string> = {
  BTC: "BINANCE:BTCUSDT",
  ETH: "BINANCE:ETHUSDT",
  SOL: "BINANCE:SOLUSDT",
  XRP: "BINANCE:XRPUSDT",
  BNB: "BINANCE:BNBUSDT",
  DOGE: "BINANCE:DOGEUSDT",
  VVV: "COINBASE:VVVUSD",  // Venice — not on Binance
};

function getTradingViewUrl(symbol: string) {
  const tvSymbol = TV_SYMBOL_MAP[symbol] ?? `BINANCE:${symbol}USDT`;
  return `https://www.tradingview.com/chart/?symbol=${tvSymbol}`;
}

export function PositionCard({ position }: { position: PositionRow }) {
  const tvUrl = getTradingViewUrl(position.symbol);
  const pnl = position.unrealizedPnl;

  return (
    <Card>
      <CardContent className="flex items-center justify-between pt-6">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <a
              href={tvUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-lg font-bold font-mono hover:text-blue-400 transition-colors"
              title={`Open ${position.symbol}/USDT on TradingView`}
            >
              {position.symbol} ↗
            </a>
            <Badge variant={position.side === "long" ? "default" : "secondary"}>
              {position.side.toUpperCase()}
            </Badge>
            {position.source === "db" && (
              <Badge variant="outline" className="text-[10px] text-yellow-500 border-yellow-500/40">
                DB
              </Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground font-mono">
            {position.size.toFixed(6)} @ ${position.entryPrice.toLocaleString()}
          </p>
          <p className="text-xs text-muted-foreground">
            {position.strategies.length > 0
              ? `Strategy: ${position.strategies.join(", ")}`
              : "Strategy: unknown"}
          </p>
          {position.liquidationPrice != null && position.liquidationPrice > 0 && (
            <p className="text-xs text-red-400/70">
              Liq: ${position.liquidationPrice.toLocaleString()}
            </p>
          )}
        </div>
        <div className="text-right">
          <p
            className={`text-lg font-bold font-mono ${
              pnl >= 0 ? "text-green-500" : "text-red-500"
            }`}
          >
            {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
          </p>
          <p className="text-xs text-muted-foreground">Unrealized P&L</p>
        </div>
      </CardContent>
    </Card>
  );
}
