"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import { Switch } from "@/components/ui/switch";

import { type Mode, withMode } from "@/lib/mode";

/** Operator-limit refusals from the toggle route. Without this the
 *  switch just bounces back with no explanation, which is what a
 *  tenant sees the moment `max_active_strategies` or
 *  `allowed_strategies` is set on them. */
const REFUSAL_MESSAGES: Record<string, string> = {
  "strategy-cap-exceeded": "At your strategy limit",
  "strategy-not-allowed": "Not enabled for your account",
  "strategy-cap-unverifiable": "Couldn't check your strategy limit",
};

export function StrategyToggle({ name, mode }: { name: string; mode: Mode }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [refusal, setRefusal] = useState<string>("");
  const [isPending, startTransition] = useTransition();

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(withMode("/api/control/config", mode), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      setEnabled(!data.disabled_strategies.includes(name));
    } catch {
      // ignore
    }
  }, [name, mode]);

  // `refresh` is async: the setState lands in the resolved promise,
  // never synchronously in the effect body. Kicking off the first
  // poll through the same callback the interval uses keeps that
  // explicit (and satisfies react-hooks/set-state-in-effect).
  useEffect(() => {
    const poll = () => void refresh();
    poll();
    const t = setInterval(poll, 10_000);
    return () => clearInterval(t);
  }, [refresh]);

  function toggle(next: boolean) {
    setEnabled(next);
    setRefusal("");
    startTransition(async () => {
      const res = await fetch(
        withMode(`/api/control/strategy/${encodeURIComponent(name)}/toggle`, mode),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        }
      ).catch(() => null);
      if (res && !res.ok) {
        const body = (await res.json().catch(() => ({}))) as {
          error?: string;
        };
        setRefusal(REFUSAL_MESSAGES[body.error ?? ""] ?? "");
      }
      // refresh() re-reads the bot, so a refused toggle puts the
      // switch back where it belongs on its own.
      await refresh();
    });
  }

  return (
    <div className="flex items-center gap-2">
      <Switch
        checked={enabled ?? true}
        onCheckedChange={toggle}
        disabled={isPending || enabled === null}
      />
      <span className="text-xs text-muted-foreground">
        {enabled === null ? "..." : enabled ? "On" : "Off"}
      </span>
      {refusal && (
        <span role="alert" className="text-xs text-yellow-400">
          {refusal}
        </span>
      )}
    </div>
  );
}
