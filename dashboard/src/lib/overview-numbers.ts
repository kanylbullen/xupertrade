import type { DailyPnl } from "./queries";

/**
 * Pure arithmetic behind the overview's headline cards, kept apart from
 * the page so it can be tested without a database.
 *
 * Two of those cards used to report something other than their label:
 * "Today" was the realized P&L of the last day that had any trades
 * (possibly weeks ago, and without funding), and "Equity P&L since first
 * snapshot" measured from the oldest of the latest 200 snapshots, about
 * three hours at one snapshot a minute.
 */

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

/**
 * How far the first snapshot inside a window may start after the
 * window's nominal start before the change is labelled with its real
 * start instead ("since …"). Snapshots are written every tick, so a
 * gap longer than this means the bot was not running.
 */
export const SNAPSHOT_SLACK_MS = 15 * 60 * 1000;

export const EQUITY_WINDOWS = [
  { label: "24h", ms: DAY_MS },
  { label: "7d", ms: 7 * DAY_MS },
  { label: "30d", ms: 30 * DAY_MS },
] as const;

export type EquityPoint = { equity: number; at: Date };

export type EquityChange = {
  label: string;
  /** latest − baseline; null when no usable snapshot falls inside the
   * window. */
  change: number | null;
  pct: number | null;
  /** Set when the change covers less than `label` says, with `until`:
   * it runs from `since` (the baseline) … */
  since: Date | null;
  /** … to `until`, the latest snapshot, when that is older than now:
   * a stopped bot's "24h" is the few hours it ran in that window.
   * Null when the latest snapshot is current, i.e. the change runs to
   * now. */
  until: Date | null;
};

export function equityChange(
  label: string,
  windowMs: number,
  latest: EquityPoint | null,
  baseline: EquityPoint | null,
  nowMs: number,
): EquityChange {
  // A zero baseline is a failed read (queries.ts skips those too), and
  // a change measured from it would be the whole equity.
  if (!latest || !baseline || baseline.equity <= 0) {
    return { label, change: null, pct: null, since: null, until: null };
  }
  const change = latest.equity - baseline.equity;
  const pct = (change / baseline.equity) * 100;
  const late = baseline.at.getTime() - (nowMs - windowMs) > SNAPSHOT_SLACK_MS;
  const stale = nowMs - latest.at.getTime() > SNAPSHOT_SLACK_MS;
  return {
    label,
    change,
    pct,
    since: late || stale ? baseline.at : null,
    until: stale ? latest.at : null,
  };
}

/** The UTC calendar day of `nowMs`, as "YYYY-MM-DD". */
export function utcDay(nowMs: number): string {
  return new Date(nowMs).toISOString().slice(0, 10);
}

/** Today's (UTC) row from `getDailyPnl`, or null when nothing was
 * realized and no funding was booked today. */
export function todayRow(rows: DailyPnl[], nowMs: number): DailyPnl | null {
  const today = utcDay(nowMs);
  return rows.find((r) => r.date === today) ?? null;
}
