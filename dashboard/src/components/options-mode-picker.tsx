"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";

import { type Mode, MODES, isValidMode } from "@/lib/mode";

const STORAGE_KEY = "options.mode";

/**
 * Mode picker for the Options page. Options are per-bot (each bot
 * has its own paused/disabled-strategies/leverage state in Redis),
 * so unlike the Trades filter there's no "all modes" option — you
 * pick exactly one bot.
 *
 * Source-of-truth precedence:
 *   1. `?mode=<paper|testnet|mainnet>` in the URL (page already
 *      reads this server-side; this picker just rewrites it).
 *   2. `localStorage["options.mode"]` (operator's last pick).
 *   3. Default = "paper".
 *
 * Why this exists: Copilot review of PR #105 caught that linking
 * to `/options` from a sidebar without a global mode toggle silently
 * lands the operator on paper-mode controls. Sticky localStorage +
 * an explicit picker keeps mode-aware Settings useful in the new
 * route-bound nav.
 */
export function OptionsModePicker({ active }: { active: Mode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // On mount: if the URL has no `?mode=`, hydrate from localStorage.
  // Replace (not push) so the back button doesn't trap on the
  // un-moded URL.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (searchParams.get("mode")) return;
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (isValidMode(saved) && saved !== "paper") {
      router.replace(`${pathname}?mode=${saved}`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pick(m: Mode) {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, m);
    }
    router.push(`${pathname}?mode=${m}`);
  }

  return (
    <div
      role="group"
      aria-label="Select bot mode for Options"
      className="inline-flex items-center rounded-lg border bg-muted/30 p-1 text-xs"
    >
      {MODES.map((m) => {
        const isActive = m === active;
        return (
          <button
            key={m}
            type="button"
            onClick={() => pick(m)}
            aria-pressed={isActive}
            className={
              isActive
                ? "rounded-md bg-background px-3 py-1 font-medium text-foreground shadow-sm"
                : "rounded-md px-3 py-1 text-muted-foreground transition-colors hover:text-foreground"
            }
          >
            {m[0].toUpperCase() + m.slice(1)}
          </button>
        );
      })}
    </div>
  );
}
