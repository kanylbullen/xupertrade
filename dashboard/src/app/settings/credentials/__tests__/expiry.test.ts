/**
 * Stability tests for the expiry-badge day-count.
 *
 * Regression: the original implementation used
 *   Math.round((expires - now) / 86400000)
 * which silently jittered between e.g. "5d" and "4d" depending on
 * the current time-of-day. UTC-midnight diffing fixes that.
 */

import { describe, expect, it } from "vitest";

import { formatExpiryBadge, utcDayDiff } from "../expiry";

describe("formatExpiryBadge — day-count stability", () => {
  it("returns the same badge at 00:01 UTC and 23:59 UTC on the same day", () => {
    // Expiry: 2027-01-15 (5d away from 2027-01-10 UTC).
    const iso = "2027-01-15T00:00:00.000Z";
    const earlyMorning = new Date("2027-01-10T00:01:00.000Z");
    const lateNight = new Date("2027-01-10T23:59:00.000Z");

    const earlyBadge = formatExpiryBadge(iso, earlyMorning);
    const lateBadge = formatExpiryBadge(iso, lateNight);

    expect(earlyBadge).toEqual(lateBadge);
    expect(earlyBadge.text).toBe("Expires 2027-01-15 (5d)");
    expect(earlyBadge.tone).toBe("warn"); // 5d ≤ 14 → warn
  });

  it("classifies a past expiry as expired regardless of how rounding falls", () => {
    // Expiry instant is in the past — even a few seconds ago must
    // read as "Expired", never as a non-negative day-count.
    const now = new Date("2027-01-10T12:00:00.000Z");
    const justElapsed = new Date(now.getTime() - 60_000).toISOString();

    const badge = formatExpiryBadge(justElapsed, now);
    expect(badge.tone).toBe("bad");
    expect(badge.text).toMatch(/^Expired \d+d ago$/);
  });

  it("uses 'warn' tone within the 14-day window and 'ok' beyond", () => {
    const now = new Date("2027-01-10T08:00:00.000Z");
    expect(formatExpiryBadge("2027-01-24T00:00:00Z", now).tone).toBe("warn"); // 14d
    expect(formatExpiryBadge("2027-01-25T00:00:00Z", now).tone).toBe("ok"); // 15d
  });

  it("utcDayDiff is symmetric about UTC midnight", () => {
    const a = new Date("2027-01-10T00:00:00Z");
    const b = new Date("2027-01-15T00:00:00Z");
    expect(utcDayDiff(a, b)).toBe(5);
    expect(utcDayDiff(b, a)).toBe(-5);
  });
});
