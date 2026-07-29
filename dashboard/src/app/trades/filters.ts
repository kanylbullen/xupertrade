/**
 * Query-param parsing for the Trades page, split out of page.tsx so it
 * can be unit-tested without rendering a server component.
 */

/**
 * Parse a `YYYY-MM-DD` query param into a UTC Date at midnight.
 *
 * Returns undefined for anything malformed rather than throwing — a
 * hand-edited or stale URL should degrade to "no date constraint", not
 * a 500. Note `new Date("2026-02-31T00:00:00Z")` does NOT throw; it
 * rolls over to March 3rd, so the round-trip is checked rather than
 * trusting the parse.
 */
export function parseDateParam(v: string | undefined): Date | undefined {
  if (!v || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return undefined;
  const d = new Date(`${v}T00:00:00.000Z`);
  if (Number.isNaN(d.getTime())) return undefined;
  // Reject rolled-over dates (2026-02-31 -> 2026-03-03).
  return d.toISOString().slice(0, 10) === v ? d : undefined;
}

/**
 * The `to` filter is inclusive in the UI ("trades up to and including
 * this day") but exclusive in SQL, so the caller gets the day after.
 *
 * Without this, from=to=2026-07-01 matches only the midnight instant
 * and returns an empty table for a day that has trades.
 */
export function exclusiveEnd(v: string | undefined): Date | undefined {
  const d = parseDateParam(v);
  return d ? new Date(d.getTime() + 24 * 60 * 60 * 1000) : undefined;
}

/** Clamp a `?page=` param to a sane 1-based page number. */
export function parsePageParam(v: string | undefined): number {
  const n = Number.parseInt(v ?? "1", 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}
