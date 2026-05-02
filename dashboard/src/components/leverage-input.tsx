"use client";

import { useEffect, useState, useTransition } from "react";
import { useMode, withMode } from "@/lib/use-mode";

type Info = { default: number; current: number; overridden: boolean };

export function LeverageInput({ name }: { name: string }) {
  const mode = useMode();
  const [info, setInfo] = useState<Info | null>(null);
  const [draft, setDraft] = useState<number | null>(null);
  const [isPending, startTransition] = useTransition();

  async function refresh() {
    try {
      const res = await fetch(withMode("/api/control/config", mode), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const lev: Info | undefined = data.leverage?.[name];
      if (lev) {
        setInfo(lev);
        if (draft === null) setDraft(lev.current);
      }
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, [name, mode]);

  function commit(value: number) {
    if (!info) return;
    if (value === info.current) return;
    startTransition(async () => {
      await fetch(
        withMode(`/api/control/strategy/${encodeURIComponent(name)}/leverage`, mode),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ leverage: value }),
        }
      ).catch(() => null);
      await refresh();
    });
  }

  function reset() {
    startTransition(async () => {
      await fetch(
        withMode(`/api/control/strategy/${encodeURIComponent(name)}/leverage`, mode),
        { method: "DELETE" }
      ).catch(() => null);
      await refresh();
    });
  }

  if (!info) {
    return <span className="text-xs text-muted-foreground">…</span>;
  }

  return (
    <div className="flex items-center gap-2">
      <input
        type="number"
        min={1}
        max={50}
        step={1}
        value={draft ?? info.current}
        disabled={isPending}
        onChange={(e) => setDraft(Number(e.target.value))}
        onBlur={(e) => commit(Number(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.currentTarget.blur();
          }
        }}
        className="w-14 rounded border bg-background px-2 py-0.5 text-sm font-mono"
      />
      <span className="text-xs text-muted-foreground">×</span>
      {info.overridden && (
        <button
          onClick={reset}
          disabled={isPending}
          className="text-[10px] text-muted-foreground underline hover:text-foreground"
          title={`Default: ${info.default}x`}
        >
          reset
        </button>
      )}
      {!info.overridden && (
        <span
          className="text-[10px] text-muted-foreground"
          title="Strategy default"
        >
          (default)
        </span>
      )}
    </div>
  );
}
