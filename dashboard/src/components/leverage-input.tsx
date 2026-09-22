"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

import { type Mode, withMode } from "@/lib/mode";

type Info = { default: number; current: number; overridden: boolean };

export function LeverageInput({ name, mode }: { name: string; mode: Mode }) {
  const [info, setInfo] = useState<Info | null>(null);
  const [draft, setDraft] = useState<number | null>(null);
  const [isPending, startTransition] = useTransition();

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(withMode("/api/control/config", mode), { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const lev: Info | undefined = data.leverage?.[name];
      if (lev) {
        setInfo(lev);
        // Functional update so `draft` stays out of the deps — it
        // changes on every keystroke, and depending on it would
        // re-create `refresh` and tear down the poll interval while
        // the operator is still typing. Semantics are unchanged:
        // seed the input once, never clobber what's being typed.
        setDraft((d) => (d === null ? lev.current : d));
      }
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
      // Drop the draft so the refresh below re-seeds the input with
      // the strategy default the DELETE just restored. Without this
      // the box would keep showing the override we just removed, and
      // the next blur would POST it straight back.
      setDraft(null);
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
