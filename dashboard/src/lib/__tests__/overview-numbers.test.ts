import { describe, expect, it } from "vitest";

import {
  EQUITY_WINDOWS,
  SNAPSHOT_SLACK_MS,
  equityChange,
  todayRow,
  utcDay,
} from "../overview-numbers";
import type { DailyPnl } from "../queries";

const NOW = Date.parse("2026-09-25T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;

function day(date: string, realizedPnl: number, funding = 0): DailyPnl {
  return {
    date,
    realizedPnl,
    fees: 0,
    entryFees: 0,
    trades: realizedPnl === 0 ? 0 : 1,
    funding,
    net: realizedPnl + funding,
  };
}

describe("todayRow", () => {
  it("is today's UTC row, net of funding", () => {
    const rows = [day("2026-09-24", 50), day("2026-09-25", -20, -1.5)];
    expect(todayRow(rows, NOW)?.net).toBe(-21.5);
  });

  it("is null when the last active day was not today", () => {
    // The card used to show the last row's realized P&L, i.e. whatever
    // happened on the most recent day with trades, however old.
    const rows = [day("2026-09-01", 300), day("2026-09-20", -40)];
    expect(todayRow(rows, NOW)).toBeNull();
  });

  it("uses the UTC calendar day", () => {
    // 23:30 UTC on the 24th is already the 25th in Stockholm.
    const lateEvening = Date.parse("2026-09-24T23:30:00Z");
    expect(utcDay(lateEvening)).toBe("2026-09-24");
    const rows = [day("2026-09-25", 10)];
    expect(todayRow(rows, lateEvening)).toBeNull();
  });
});

describe("equityChange", () => {
  const latest = { equity: 9_500, at: new Date(NOW - 60_000) };

  it("is latest minus the baseline, signed, with a percentage", () => {
    const baseline = { equity: 10_000, at: new Date(NOW - DAY + 30_000) };
    const c = equityChange("24h", DAY, latest, baseline, NOW);
    expect(c).toEqual({ label: "24h", change: -500, pct: -5, since: null, until: null });
  });

  it("states the real start when the window is only partly covered", () => {
    const threeDaysAgo = new Date(NOW - 3 * DAY);
    const c = equityChange("7d", 7 * DAY, latest, { equity: 9_000, at: threeDaysAgo }, NOW);
    expect(c.change).toBe(500);
    expect(c.since).toEqual(threeDaysAgo);
  });

  it("tolerates snapshot jitter at the window edge", () => {
    const at = new Date(NOW - DAY + SNAPSHOT_SLACK_MS - 1);
    expect(equityChange("24h", DAY, latest, { equity: 1, at }, NOW).since).toBeNull();
  });

  it("has no number without a snapshot in the window, or without any", () => {
    expect(equityChange("24h", DAY, latest, null, NOW).change).toBeNull();
    expect(equityChange("24h", DAY, null, null, NOW).change).toBeNull();
  });

  it("has no number against a zero baseline", () => {
    // A $0 snapshot is a failed balance read (pre-#167 bots wrote one
    // on every HL 502): measured from it, the whole equity would show
    // as the window's gain.
    const c = equityChange("24h", DAY, latest, { equity: 0, at: new Date(NOW - DAY) }, NOW);
    expect(c.change).toBeNull();
    expect(c.pct).toBeNull();
  });

  it("ends the window at a stale latest snapshot, not now", () => {
    // The bot stopped 20h ago. The 24h baseline is on time, but the
    // change only covers the 4h the bot ran inside the window.
    const stopped = { equity: 9_500, at: new Date(NOW - 20 * 60 * 60 * 1000) };
    const baseline = { equity: 10_000, at: new Date(NOW - DAY + 30_000) };
    const c = equityChange("24h", DAY, stopped, baseline, NOW);
    expect(c.change).toBe(-500);
    expect(c.since).toEqual(baseline.at);
    expect(c.until).toEqual(stopped.at);
  });

  it("has no end date while the latest snapshot is current", () => {
    const at = new Date(NOW - SNAPSHOT_SLACK_MS + 1);
    const baseline = { equity: 10_000, at: new Date(NOW - DAY) };
    expect(equityChange("24h", DAY, { equity: 1, at }, baseline, NOW).until).toBeNull();
  });

  it("covers 24h, 7d and 30d", () => {
    expect(EQUITY_WINDOWS.map((w) => w.label)).toEqual(["24h", "7d", "30d"]);
  });
});
