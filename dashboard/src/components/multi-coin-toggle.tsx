"use client";

import { useEffect, useState, useTransition } from "react";
import { Switch } from "@/components/ui/switch";

import { type Mode, withMode } from "@/lib/mode";

export function MultiCoinToggle({ mode }: { mode: Mode }) {
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const [isPending, startTransition] = useTransition();

  async function refresh() {
    try {
      const res = await fetch(withMode("/api/control/config", mode), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      setAllowed(data.allow_multi_coin ?? false);
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, [mode]);

  function toggle(next: boolean) {
    setAllowed(next);
    startTransition(async () => {
      await fetch(withMode("/api/control/allow-multi-coin", mode), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allow: next }),
      }).catch(() => null);
      await refresh();
    });
  }

  return (
    <div className="flex items-center gap-2 shrink-0">
      <Switch
        checked={allowed ?? false}
        onCheckedChange={toggle}
        disabled={isPending || allowed === null}
      />
      <span className="text-xs text-muted-foreground w-6">
        {allowed === null ? "…" : allowed ? "On" : "Off"}
      </span>
    </div>
  );
}
