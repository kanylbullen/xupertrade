"use client";

import { useRouter, usePathname, useSearchParams } from "next/navigation";

/**
 * Offset pager for the Backtests page. Mirrors `TradesPager`: renders
 * nothing when everything fits on one page, so a tenant with a handful
 * of runs never sees dead controls.
 */
export function BacktestsPager({
  page,
  pageSize,
  total,
}: {
  page: number;
  pageSize: number;
  total: number;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;

  function go(p: number) {
    const next = new URLSearchParams(searchParams.toString());
    if (p <= 1) next.delete("page");
    else next.set("page", String(p));
    const qs = next.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  }

  const first = (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <div className="flex items-center justify-between gap-3 border-t px-4 py-3 text-sm">
      <span className="text-muted-foreground">
        {first}–{last} of {total}
      </span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => go(page - 1)}
          disabled={page <= 1}
          className="rounded-md border px-3 py-1 transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
        >
          Previous
        </button>
        <span className="text-muted-foreground">
          Page {page} of {lastPage}
        </span>
        <button
          type="button"
          onClick={() => go(page + 1)}
          disabled={page >= lastPage}
          className="rounded-md border px-3 py-1 transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  );
}
