/**
 * The overview's headline cards: an empty state instead of invented
 * numbers, a sign on every amount, windows that say what they cover,
 * and "today" meaning today.
 */
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { OverviewStats, type OverviewStatsProps } from "../overview-stats";
import { EQUITY_WINDOWS, equityChange } from "@/lib/overview-numbers";

const NOW = Date.parse("2026-09-25T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;

function text(element: ReactElement): string {
  return renderToStaticMarkup(element)
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

function noChanges() {
  return EQUITY_WINDOWS.map((w) => equityChange(w.label, w.ms, null, null, NOW));
}

function props(over: Partial<OverviewStatsProps> = {}): OverviewStatsProps {
  return {
    modeLabel: "Testnet (live)",
    dbConnected: true,
    latestEquity: null,
    equityChanges: noChanges(),
    realized: { realizedPnl: 0, fees: 0, trades: 0 },
    today: null,
    nowMs: NOW,
    ...over,
  };
}

describe("OverviewStats empty state", () => {
  it("shows no equity figure, and no $10,000 placeholder, without snapshots", () => {
    const out = text(<OverviewStats {...props()} />);
    expect(out).not.toContain("10,000");
    expect(out).not.toContain("10000");
    expect(out).toContain("Total Equity — No equity snapshots yet");
    expect(out).toContain("Equity change — No equity snapshots yet");
  });

  it("shows dashes, not zeros, when the database is unreachable", () => {
    const out = text(<OverviewStats {...props({ dbConnected: false })} />);
    expect(out).not.toContain("$0.00");
    expect(out).toContain("Realized P&L (all-time) — DB offline");
    expect(out).toContain("Today's P&L (UTC) — DB offline");
  });

  it("says when nothing happened today", () => {
    const out = text(<OverviewStats {...props()} />);
    expect(out).toContain("Today's P&L (UTC) $0.00 No trades or funding yet today");
  });
});

describe("OverviewStats amounts", () => {
  const latest = { equity: 9_512.34, at: new Date(NOW - 60_000) };
  const baselines = [
    { equity: 10_000, at: new Date(NOW - DAY + 60_000) },
    { equity: 9_000, at: new Date(NOW - 7 * DAY + 60_000) },
    { equity: 12_000, at: new Date(NOW - 30 * DAY + 60_000) },
  ];
  const changes = EQUITY_WINDOWS.map((w, i) =>
    equityChange(w.label, w.ms, latest, baselines[i], NOW),
  );

  it("signs every amount, losses included", () => {
    const out = text(
      <OverviewStats
        {...props({
          latestEquity: latest,
          equityChanges: changes,
          realized: { realizedPnl: -123.456, fees: 7.5, trades: 12 },
          today: {
            date: "2026-09-25",
            realizedPnl: -20,
            fees: 1,
            trades: 2,
            funding: -0.75,
            net: -20.75,
          },
        })}
      />,
    );
    expect(out).toContain("Total Equity $9,512.34 Testnet (live)");
    expect(out).toContain("24h -$487.66 (-4.88%)");
    expect(out).toContain("7d +$512.34 (+5.69%)");
    expect(out).toContain("30d -$2,487.66 (-20.73%)");
    expect(out).toContain("Realized P&L (all-time) -$123.46 12 trades · fees -$7.50");
    expect(out).toContain(
      "Today's P&L (UTC) -$20.75 Realized -$20.00 · funding -$0.75 · 2 trades",
    );
    expect(out).not.toMatch(/\$-/);
  });

  it("labels a partly covered window with its real start, once", () => {
    // The bot has only written snapshots for two days: 7d and 30d both
    // fall back to the same first snapshot.
    const first = { equity: 9_000, at: new Date(Date.parse("2026-09-23T08:00:00Z")) };
    const partial = EQUITY_WINDOWS.map((w, i) =>
      equityChange(w.label, w.ms, latest, i === 0 ? baselines[0] : first, NOW),
    );
    const out = text(<OverviewStats {...props({ latestEquity: latest, equityChanges: partial })} />);
    // Europe/Stockholm, as everywhere else on the dashboard.
    const since = "since 2026-09-23 10:00:00 +$512.34 (+5.69%)";
    expect(out).toContain("24h -$487.66");
    expect(out).toContain(since);
    expect(out.split(since)).toHaveLength(2);
    expect(out).not.toContain("7d");
    expect(out).not.toContain("30d");
  });

  it("says how old the equity figure is when the bot stopped writing", () => {
    const stale = { equity: 9_512.34, at: new Date(NOW - 3 * 60 * 60 * 1000) };
    const out = text(<OverviewStats {...props({ latestEquity: stale })} />);
    expect(out).toContain("Testnet (live) · last snapshot 2026-09-25 11:00:00");
    expect(out).toContain("24h no snapshot");
  });
});
