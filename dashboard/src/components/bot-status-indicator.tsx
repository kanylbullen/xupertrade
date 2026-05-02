"use client";

import { useEffect, useState } from "react";
import { useMode, withMode } from "@/lib/use-mode";

type BotState = "loading" | "running" | "paused" | "offline";

export function BotStatusIndicator() {
  const mode = useMode();
  const [state, setState] = useState<BotState>("loading");

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const res = await fetch(withMode("/api/control/state", mode), {
          cache: "no-store",
          signal: AbortSignal.timeout(4000),
        });
        if (!res.ok) {
          if (!cancelled) setState("offline");
          return;
        }
        const data = await res.json();
        if (!cancelled) setState(data.paused ? "paused" : "running");
      } catch {
        if (!cancelled) setState("offline");
      }
    }

    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [mode]);

  if (state === "loading") {
    return (
      <span
        className="inline-flex h-2 w-2 rounded-full bg-muted-foreground/40"
        title="Checking bot status…"
      />
    );
  }

  if (state === "offline") {
    return (
      <span className="inline-flex items-center gap-1" title="Bot unreachable">
        <span className="inline-flex h-2 w-2 rounded-full bg-red-500" />
        <span className="text-xs font-normal text-red-400">Offline</span>
      </span>
    );
  }

  if (state === "paused") {
    return (
      <span className="inline-flex items-center gap-1" title="Bot paused">
        <span className="inline-flex h-2 w-2 rounded-full bg-yellow-500 animate-pulse" />
        <span className="text-xs font-normal text-yellow-400">Paused</span>
      </span>
    );
  }

  return (
    <span
      className="inline-flex h-2 w-2 rounded-full bg-green-500"
      title="Bot running"
    />
  );
}
