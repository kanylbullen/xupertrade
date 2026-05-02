"use client";

import { useEffect, useRef, useState } from "react";

type LogEvent = {
  type: string;
  timestamp: string;
  [key: string]: unknown;
};

function formatEvent(event: LogEvent): string {
  const ts = event.timestamp ? new Date(event.timestamp) : new Date();
  const time = ts.toLocaleTimeString("sv-SE", {
    timeZone: "Europe/Stockholm",
    hour12: false,
  });

  switch (event.type) {
    case "tick.completed":
      return `${time} [${event.strategy}] ${event.symbol} ${event.timeframe} — $${Number(event.price).toLocaleString()} → ${event.signal === "none" ? "hold" : `${event.signal}: ${event.reason}`}`;
    case "signal.generated":
      return `${time} [${event.strategy}] SIGNAL: ${event.action} ${event.symbol} — ${event.reason}`;
    case "trade.executed":
      return `${time} [${event.strategy}] TRADE: ${String(event.side).toUpperCase()} ${event.size} ${event.symbol} @ $${Number(event.price).toLocaleString()}`;
    case "bot.heartbeat":
      return `${time} [heartbeat] equity: $${Number(event.equity).toLocaleString()} | positions: ${event.positions} | uptime: ${formatUptime(Number(event.uptime_seconds))}`;
    case "error":
      return `${time} [ERROR] ${event.strategy}: ${event.message}`;
    case "connected":
      return `${time} [system] Connected to event stream`;
    default:
      return `${time} [${event.type}] ${JSON.stringify(event)}`;
  }
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function LiveLog() {
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let es: EventSource;
    let retryTimeout: ReturnType<typeof setTimeout>;
    let delay = 1000;

    function connect() {
      es = new EventSource("/api/events");

      es.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data) as LogEvent;
          setEvents((prev) => [...prev.slice(-199), event]);
          if (event.type === "connected") {
            setConnected(true);
            delay = 1000;
          }
        } catch {
          // ignore parse errors
        }
      };

      es.onerror = () => {
        setConnected(false);
        es.close();
        retryTimeout = setTimeout(() => {
          delay = Math.min(delay * 2, 30_000);
          connect();
        }, delay);
      };
    }

    connect();
    return () => {
      clearTimeout(retryTimeout);
      es.close();
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <div
          className={`h-2 w-2 rounded-full ${
            connected ? "bg-green-500" : "bg-red-500"
          }`}
        />
        <span className="text-xs text-muted-foreground">
          {connected ? "Connected" : "Disconnected"}
        </span>
        <span className="text-xs text-muted-foreground">
          ({events.length} events)
        </span>
      </div>
      <div className="h-[400px] overflow-y-auto rounded-lg border bg-black/50 p-3 font-mono text-xs">
        {events.length === 0 ? (
          <p className="text-muted-foreground">
            Waiting for events from bot...
          </p>
        ) : (
          events.map((event, i) => {
            const line = formatEvent(event);
            const isError = event.type === "error";
            const isTrade = event.type === "trade.executed";
            const isSignal = event.type === "signal.generated";
            return (
              <div
                key={i}
                className={`py-0.5 ${
                  isError
                    ? "text-red-400"
                    : isTrade
                      ? "text-green-400"
                      : isSignal
                        ? "text-yellow-400"
                        : "text-muted-foreground"
                }`}
              >
                {line}
              </div>
            );
          })
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
