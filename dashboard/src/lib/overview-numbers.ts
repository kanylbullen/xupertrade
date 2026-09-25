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
  /** latest − baseline; null when no snapshot falls inside the window. */
  change: number | null;
  pct: number | null;
  /** Set when the baseline starts later than the nominal window, i.e.
   * the change covers less than `label` says: it runs since this time. */
  since: Date | null;
};

export function equityChange(
  label: string,
  windowMs: number,
  latest: EquityPoint | null,
  baseline: EquityPoint | null,
  nowMs: number,
): EquityChange {
  if (!latest || !baseline) {
    return { label, change: null, pct: null, since: null };
  }
  const change = latest.equity - baseline.equity;
  const pct = baseline.equity > 0 ? (change / baseline.equity) * 100 : null;
  const late = baseline.at.getTime() - (nowMs - windowMs) > SNAPSHOT_SLACK_MS;
  return { label, change, pct, since: late ? baseline.at : null };
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
