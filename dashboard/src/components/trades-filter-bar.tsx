"use client";

import { useRouter, usePathname, useSearchParams } from "next/navigation";

/**
 * Strategy + date-range filters for the Trades page.
 *
 * All state lives in the URL so a filtered view is linkable and
 * survives refresh; the page is a server component that reads the same
 * query params. Changing any filter resets to page 1 — keeping the old
 * page number would land the operator on an out-of-range offset and
 * show an empty table for a filter that does have results.
 *
 * The mode pill is a separate component (`TradesModeFilter`) because it
 * predates this bar and persists its own choice to localStorage.
 */
export function TradesFilterBar({
  strategies,
  strategy,
  from,
  to,
}: {
  strategies: string[];
  strategy: string;
  from: string;
  to: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  function update(key: string, value: string) {
    const next = new URLSearchParams(searchParams.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    // Any filter change invalidates the current offset.
    next.delete("page");
    const qs = next.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  }

  const hasFilters = Boolean(strategy || from || to);

  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-xs">
        <span className="text-muted-foreground">Strategy</span>
        <select
          value={strategy}
          onChange={(e) => update("strategy", e.target.value)}
          className="h-8 rounded-md border bg-background px-2 text-sm"
        >
          <option value="">All strategies</option>
          {strategies.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-xs">
        <span className="text-muted-foreground">From</span>
        <input
          type="date"
          value={from}
          max={to || undefined}
          onChange={(e) => update("from", e.target.value)}
          className="h-8 rounded-md border bg-background px-2 text-sm"
        />
      </label>

      <label className="flex flex-col gap-1 text-xs">
        <span className="text-muted-foreground">To</span>
        <input
          type="date"
          value={to}
          min={from || undefined}
          onChange={(e) => update("to", e.target.value)}
          className="h-8 rounded-md border bg-background px-2 text-sm"
        />
      </label>

      {hasFilters && (
        <button
          type="button"
          onClick={() => {
            const next = new URLSearchParams(searchParams.toString());
            for (const k of ["strategy", "from", "to", "page"]) next.delete(k);
            const qs = next.toString();
            router.push(qs ? `${pathname}?${qs}` : pathname);
          }}
          className="h-8 rounded-md border px-3 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          Clear
        </button>
      )}
    </div>
  );
}
