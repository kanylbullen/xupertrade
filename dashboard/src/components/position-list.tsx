"use client";

import { useState } from "react";
import { PositionCard, type PositionRow } from "@/components/position-card";
import { TradingViewChart } from "@/components/tv-chart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const TIMEFRAME_MAP: Record<string, string> = {
  supertrend: "1d",
  rsi_momentum: "4h",
  bb_short: "1h",
  sma_rsi: "1d",
  volatility_breakout: "1h",
  keltner_breakout: "4h",
  ema_crossover: "1h",
  btc_mean_reversion: "15",
  pivot_supertrend: "4h",
  hash_momentum: "4h",
  cdc_macd: "1d",
  macd_zero: "1d",
  moon_phases: "1d",
  penguin_volatility: "1h",
};

export function PositionList({ positions }: { positions: PositionRow[] }) {
  const first = positions[0];
  const [selectedSymbol, setSelectedSymbol] = useState(first?.symbol ?? "BTC");
  const [selectedTimeframe, setSelectedTimeframe] = useState(
    TIMEFRAME_MAP[first?.strategies[0] ?? ""] ?? "4h"
  );
  const [selectedStrategy, setSelectedStrategy] = useState(
    first?.strategies[0] ?? ""
  );

  if (positions.length === 0) {
    return (
      <>
        <h2 className="text-lg font-semibold">Open Positions</h2>
        <p className="text-sm text-muted-foreground">No open positions.</p>
        <Card>
          <CardHeader>
            <CardTitle>BTC/USDT — Live Chart</CardTitle>
          </CardHeader>
          <CardContent>
            <TradingViewChart symbol="BTC" timeframe="4h" height={450} />
          </CardContent>
        </Card>
      </>
    );
  }

  return (
    <>
      <h2 className="text-lg font-semibold">
        Open Positions
        <span className="ml-2 text-xs text-muted-foreground font-normal">
          Click a position to view its chart
        </span>
      </h2>
      <div className="space-y-2">
        {positions.map((pos) => (
          <div
            key={`${pos.symbol}-${pos.side}`}
            onClick={() => {
              setSelectedSymbol(pos.symbol);
              setSelectedTimeframe(TIMEFRAME_MAP[pos.strategies[0] ?? ""] ?? "4h");
              setSelectedStrategy(pos.strategies[0] ?? "");
            }}
            className={`cursor-pointer rounded-lg transition-all ${
              selectedSymbol === pos.symbol
                ? "ring-2 ring-blue-500"
                : "hover:ring-1 hover:ring-muted-foreground/30"
            }`}
          >
            <PositionCard position={pos} />
          </div>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>
            {selectedSymbol}/USDT — {selectedTimeframe}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <TradingViewChart
            symbol={selectedSymbol}
            timeframe={selectedTimeframe}
            height={450}
            strategy={selectedStrategy}
          />
        </CardContent>
      </Card>
    </>
  );
}
