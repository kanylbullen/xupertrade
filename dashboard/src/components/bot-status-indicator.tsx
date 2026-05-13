"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";

import { type Mode, withMode } from "@/lib/mode";

type BotState = "loading" | "running" | "paused" | "offline";

/**
 * Tiny status pill for a single bot mode.
 *
 * Two render variants:
 *   - default (`variant="pill"`): the original behaviour — green dot
 *     when running, or a coloured dot + small text label for paused /
 *     offline. Source of `mode` follows the pathname (`/overview/<mode>`)
 *     with `defaultMode` as a fallback.
 *   - `variant="dot"` + explicit `mode` prop: just a coloured dot, no
 *     text, no pathname coupling. Used by the sidebar Overview group to
 *     render one dot per mode link (Paper / Testnet / Mainnet) so the
 *     operator sees at-a-glance status for all three bots, not just the
 *     one whose page they're currently on.
 *
 * After the sidebar cutover (PR C of the nav refactor) there is no
 * global `?mode=` query any more — `useSearchParams()` would always
 * return null. Reading the pathname is the new source of truth for the
 * pill variant.
 */
export function BotStatusIndicator({
  defaultMode = "testnet",
  mode: modeProp,
  variant = "pill",
}: {
  defaultMode?: Mode;
  mode?: Mode;
  variant?: "pill" | "dot";
}) {
  const pathname = usePathname();
  const routeMode = parseModeFromPath(pathname);
  // Explicit `mode` prop wins (sidebar per-row dots). Otherwise infer
  // from the current `/overview/<mode>` route, falling back to default.
  const mode: Mode = modeProp ?? routeMode ?? defaultMode;
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

  // Dot-only variant: just the coloured circle, no text label.
  // Keeps the SidebarMenuButton row compact in collapsed (icon-only)
  // sidebar mode too.
  if (variant === "dot") {
    const { dotClass, title } = dotPresentation(state, mode);
    return (
      <span
        className={`inline-flex h-2 w-2 shrink-0 rounded-full ${dotClass}`}
        title={title}
        aria-label={title}
      />
    );
  }

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

function dotPresentation(
  state: BotState,
  mode: Mode
): { dotClass: string; title: string } {
  switch (state) {
    case "running":
      return { dotClass: "bg-green-500", title: `Bot running (${mode})` };
    case "paused":
      return {
        dotClass: "bg-yellow-500 animate-pulse",
        title: `Bot paused (${mode})`,
      };
    case "offline":
      return { dotClass: "bg-red-500", title: `Bot unreachable (${mode})` };
    case "loading":
    default:
      return {
        dotClass: "bg-muted-foreground/40",
        title: `Checking bot status (${mode})…`,
      };
  }
}

function parseModeFromPath(pathname: string | null): Mode | null {
  if (!pathname) return null;
  const m = pathname.match(/^\/overview\/(paper|testnet|mainnet)(?:\/|$)/);
  return m ? (m[1] as Mode) : null;
}
