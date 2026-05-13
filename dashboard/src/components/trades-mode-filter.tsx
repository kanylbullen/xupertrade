"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";

const FILTERS = ["all", "paper", "testnet", "mainnet"] as const;
type Filter = (typeof FILTERS)[number];

const STORAGE_KEY = "trades.modeFilter";

function isFilter(v: string | null | undefined): v is Filter {
  return v === "all" || v === "paper" || v === "testnet" || v === "mainnet";
}

/**
 * Mode filter for the Trades page. Writes the chosen filter into the
 * URL (`?filter=`) and `localStorage` (`trades.modeFilter`). On first
 * load with no `?filter=` in the URL, syncs from localStorage so the
 * operator's last pick survives navigation. Default = "all" (Decision
 * 1: cross-mode default matches the new sidebar mental model).
 */
export function TradesModeFilter({ active }: { active: Filter }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Hydrate from localStorage exactly once if the URL has no filter.
  // We only run on mount; subsequent navigations carry `?filter=` in
  // the URL and own the state from there.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (searchParams.get("filter")) return;
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (isFilter(saved) && saved !== "all") {
      // Replace so the back button doesn't trap on the un-filtered URL.
      router.replace(`${pathname}?filter=${saved}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pick(f: Filter) {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, f);
    }
    if (f === "all") {
      router.push(pathname);
    } else {
      router.push(`${pathname}?filter=${f}`);
    }
  }

  return (
    <div
      role="group"
      aria-label="Filter trades by mode"
      className="inline-flex items-center rounded-lg border bg-muted/30 p-1 text-xs"
    >
      {FILTERS.map((f) => {
        const isActive = f === active;
        return (
          <button
            key={f}
            type="button"
            onClick={() => pick(f)}
            aria-pressed={isActive}
            className={
              isActive
                ? "rounded-md bg-background px-3 py-1 font-medium text-foreground shadow-sm"
                : "rounded-md px-3 py-1 text-muted-foreground transition-colors hover:text-foreground"
            }
          >
            {f === "all" ? "All modes" : f[0].toUpperCase() + f.slice(1)}
          </button>
        );
      })}
    </div>
  );
}
