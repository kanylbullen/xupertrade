/**
 * Pure helpers for the credentials UI's expiry-badge logic.
 *
 * Extracted from credentials-client.tsx so it can be unit-tested
 * without dragging in React / "use client" boundaries.
 */

export type BadgeTone = "ok" | "warn" | "bad";
export type ExpiryBadge = { text: string; tone: BadgeTone };

/**
 * Days between two instants, computed against UTC calendar midnights
 * so the result doesn't jitter as wall-clock time moves through the
 * day. Returns a signed integer: positive = `b` is later than `a`.
 */
export function utcDayDiff(a: Date, b: Date): number {
  const aMid = Date.UTC(a.getUTCFullYear(), a.getUTCMonth(), a.getUTCDate());
  const bMid = Date.UTC(b.getUTCFullYear(), b.getUTCMonth(), b.getUTCDate());
  return Math.round((bMid - aMid) / 86400000);
}

/**
 * Format the expiry badge for an HL key.
 *
 * Stability contract: the badge classification depends ONLY on the
 * UTC calendar date of `now` vs. the expiry instant, not the
 * time-of-day. The exception is when the expiry instant is in the
 * past — that case is ALWAYS classified as expired, regardless of
 * how the day-diff rounds (otherwise a key whose instant just
 * elapsed could read "0d" until UTC midnight rolls over).
 *
 * `now` is injectable so tests can pin wall-clock time.
 */
export function formatExpiryBadge(
  iso: string,
  now: Date = new Date(),
): ExpiryBadge {
  const expires = new Date(iso);
  // Hard floor: any expiry instant in the past is expired, period.
  // The day-diff below would otherwise return 0 for "expired earlier
  // today UTC" and we'd misclassify it as still-valid.
  if (expires.getTime() < now.getTime()) {
    const daysAgo = Math.max(1, -utcDayDiff(now, expires));
    return { text: `Expired ${daysAgo}d ago`, tone: "bad" };
  }
  const days = utcDayDiff(now, expires);
  return {
    text: `Expires ${expires.toISOString().slice(0, 10)} (${days}d)`,
    tone: days <= 14 ? "warn" : "ok",
  };
}
