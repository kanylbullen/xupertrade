"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";

import { type Mode, withMode } from "@/lib/mode";

type BotState = "loading" | "running" | "paused" | "offline";

/**
 * Tiny status pill. Source of `mode`:
 *   - If pathname matches `/overview/<mode>`, use that route param —
 *     the indicator tracks the page the operator is looking at.
 *   - Otherwise fall back to the `defaultMode` prop (the sidebar passes
 *     `"testnet"` so the global indicator reflects the bot the operator
 *     spends most time on).
 *
 * After the sidebar cutover (PR C of the nav refactor) there is no
 * global `?mode=` query any more — `useSearchParams()` would always
 * return null. Reading the pathname is the new source of truth.
 */
export function BotStatusIndicator({
  defaultMode = "testnet",
}: {
  defaultMode?: Mode;
}) {
  const pathname = usePathname();
  const routeMode = parseModeFromPath(pathname);
  const mode: Mode = routeMode ?? defaultMode;
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
      <span className="inline-flex items-center gap-1" title={`Bot unreachable (${mode})`}>
        <span className="inline-flex h-2 w-2 rounded-full bg-red-500" />
        <span className="text-xs font-normal text-red-400">Offline</span>
      </span>
    );
  }

  if (state === "paused") {
    return (
      <span className="inline-flex items-center gap-1" title={`Bot paused (${mode})`}>
        <span className="inline-flex h-2 w-2 rounded-full bg-yellow-500 animate-pulse" />
        <span className="text-xs font-normal text-yellow-400">Paused</span>
      </span>
    );
  }

  return (
    <span
      className="inline-flex h-2 w-2 rounded-full bg-green-500"
      title={`Bot running (${mode})`}
    />
  );
}

function parseModeFromPath(pathname: string | null): Mode | null {
  if (!pathname) return null;
  const m = pathname.match(/^\/overview\/(paper|testnet|mainnet)(?:\/|$)/);
  return m ? (m[1] as Mode) : null;
}
