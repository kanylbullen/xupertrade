"use client";

import { useCallback, useEffect, useState, useTransition } from "react";
import { Switch } from "@/components/ui/switch";

import { type Mode, withMode } from "@/lib/mode";

export function StrategyToggle({ name, mode }: { name: string; mode: Mode }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
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
    startTransition(async () => {
      await fetch(
        withMode(`/api/control/strategy/${encodeURIComponent(name)}/toggle`, mode),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: next }),
        }
      ).catch(() => null);
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
    </div>
  );
}
