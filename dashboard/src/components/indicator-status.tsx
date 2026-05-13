"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Mode = "paper" | "testnet" | "mainnet";

function withMode(path: string, mode: Mode): string {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}mode=${mode}`;
}

type Status = {
  name: string;
  symbol: string;
  timeframe: string;
  price: number;
  signal: "flat" | "long" | "short" | "ready_long" | "ready_short";
  distance_pct: number;
  description: string;
  details: Record<string, number | boolean>;
  has_open_position: boolean;
  position_side: string;
};

const STALE_MS = 2 * 60 * 1000; // 2 minutes

function signalColor(signal: Status["signal"]): string {
  switch (signal) {
    case "ready_long":
      return "bg-green-500/20 text-green-400 border-green-500/40";
    case "ready_short":
      return "bg-red-500/20 text-red-400 border-red-500/40";
    case "long":
      return "bg-blue-500/20 text-blue-400 border-blue-500/40";
    case "short":
      return "bg-orange-500/20 text-orange-400 border-orange-500/40";
    default:
      return "bg-muted/50 text-muted-foreground";
  }
}

function signalLabel(signal: Status["signal"]): string {
  switch (signal) {
    case "ready_long":  return "READY: LONG";
    case "ready_short": return "READY: SHORT";
    case "long":        return "Holding Long";
    case "short":       return "Holding Short";
    default:            return "Waiting";
  }
}

export function IndicatorStatus({ mode }: { mode: Mode }) {
  const [statuses, setStatuses] = useState<Status[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // Tick every 15 s to recompute "stale" without a full reload
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const res = await fetch(withMode("/api/indicator-status", mode));
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data = await res.json();
        if (!cancelled) {
          setStatuses(data.strategies ?? []);
          setError(null);
          setLastUpdate(new Date());
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Unknown error");
      }
    }

    load();
    const interval = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [mode]);

  const isStale = lastUpdate !== null && now - lastUpdate.getTime() > STALE_MS;

  if (error && statuses.length === 0) {
    return (
      <div className="rounded-lg border border-red-500/30 bg-red-500/5 p-4 text-sm text-red-400">
        Unable to load indicator status: {error}
      </div>
    );
  }

  if (statuses.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">Loading indicator status…</div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Header row */}
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>
          How close each strategy is to triggering
          {statuses.length < 14 && (
            <span className="ml-2 text-yellow-400">
              ({statuses.length}/14 loaded)
            </span>
          )}
        </span>
        <span className={isStale ? "text-yellow-400" : ""}>
          {lastUpdate
            ? `Updated ${lastUpdate.toLocaleTimeString("sv-SE", {
                timeZone: "Europe/Stockholm",
                hour12: false,
              })}${isStale ? " — data may be stale" : ""}`
            : null}
        </span>
      </div>

      {/* Stale banner */}
      {isStale && (
        <div className="rounded-lg border border-yellow-500/30 bg-yellow-500/5 px-4 py-2 text-xs text-yellow-400">
          Data is over 2 minutes old — bot may be offline or unresponsive.
        </div>
      )}

      {/* Cards */}
      <div className="grid gap-3 md:grid-cols-2">
        {statuses.map((s) => (
          <Card key={s.name} className={isStale ? "opacity-60" : ""}>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">{s.name}</CardTitle>
                <Badge variant="outline" className={signalColor(s.signal)}>
                  {signalLabel(s.signal)}
                </Badge>
              </div>
              <div className="flex gap-2 text-xs text-muted-foreground font-mono">
                <span>{s.symbol}</span>
                <span>{s.timeframe}</span>
                <span>
                  ${s.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                </span>
              </div>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-sm">{s.description}</p>

              {s.distance_pct > 0 && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-muted-foreground">
                    <span>Distance to trigger</span>
                    <span className="font-mono">{s.distance_pct.toFixed(2)}%</span>
                  </div>
                  <div className="h-2 rounded-full bg-muted overflow-hidden">
                    <div
                      className="h-full bg-gradient-to-r from-blue-500 to-green-500 transition-all"
                      style={{
                        width: `${Math.max(0, 100 - Math.min(s.distance_pct * 4, 100))}%`,
                      }}
                    />
                  </div>
                </div>
              )}

              <div className="flex flex-wrap gap-1 pt-1">
                {Object.entries(s.details).map(([key, value]) => (
                  <Badge key={key} variant="outline" className="font-mono text-[10px]">
                    {key}:{" "}
                    {typeof value === "number"
                      ? value.toLocaleString(undefined, { maximumFractionDigits: 2 })
                      : String(value)}
                  </Badge>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
