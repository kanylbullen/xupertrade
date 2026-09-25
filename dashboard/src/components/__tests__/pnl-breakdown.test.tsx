/**
 * Negative amounts keep their minus. PnlSummary's rows used to render
 * `sign + Math.abs(value)` with `sign` empty for a loss, so -$42.10
 * appeared as "$42.10" (in red, the only clue); the tables rendered
 * "$-42.10".
 */
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import {
  DailyPnlTable,
  PnlSummary,
  StrategyPnlTable,
} from "../pnl-breakdown";

/** The rendered text with tags stripped, whitespace collapsed. */
function text(element: ReactElement): string {
  return renderToStaticMarkup(element)
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

describe("PnlSummary", () => {
  it("shows every negative amount with its minus", () => {
    const out = text(
      <PnlSummary realized={-42.1} fees={3.5} funding={-1.25} unrealized={-7} />,
    );
    expect(out).toContain("Realized (after fees) -$42.10");
    expect(out).toContain("Unrealized (open positions) -$7.00");
    expect(out).toContain("Funding (cumulative) -$1.25");
    expect(out).toContain("Fees (in realized) -$3.50");
    expect(out).toContain("Net P&L -$43.35");
    expect(out).toContain("Total inc. unrealized -$50.35");
    expect(out).not.toMatch(/\$-/);
  });

  it("shows a net fee rebate as a gain, not as fees paid", () => {
    const out = text(<PnlSummary realized={10} fees={-0.4} funding={0} unrealized={0} />);
    expect(out).toContain("Fees (in realized) +$0.40");
  });

  it("marks gains with a plus", () => {
    const out = text(<PnlSummary realized={5} fees={0} funding={2} unrealized={0} />);
    expect(out).toContain("Net P&L +$7.00");
  });
});

describe("StrategyPnlTable", () => {
  it("signs realized P&L and shows fees as a cost", () => {
    const out = text(
      <StrategyPnlTable
        rows={[
          { strategyName: "loser", trades: 4, wins: 1, losses: 3, realizedPnl: -18.4, fees: 2.2 },
          { strategyName: "winner", trades: 2, wins: 2, losses: 0, realizedPnl: 9, fees: 0.8 },
        ]}
      />,
    );
    expect(out).toContain("loser 4 1 / 3 25% -$2.20 -$18.40");
    expect(out).toContain("winner 2 2 / 0 100% -$0.80 +$9.00");
  });
});

describe("DailyPnlTable", () => {
  it("signs each day's net and states the window", () => {
    const out = text(
      <DailyPnlTable
        windowDays={30}
        rows={[
          { date: "2026-09-24", realizedPnl: -12, fees: 1, trades: 3, funding: -0.5, net: -12.5 },
          { date: "2026-09-25", realizedPnl: 4, fees: 0.2, trades: 1, funding: 0, net: 4 },
        ]}
      />,
    );
    expect(out).toContain("2 days with activity in the last 30 days");
    expect(out).toContain("2026-09-24 3t -$12.50");
    expect(out).toContain("2026-09-25 1t +$4.00");
    expect(renderToStaticMarkup(
      <DailyPnlTable
        windowDays={30}
        rows={[{ date: "2026-09-24", realizedPnl: -12, fees: 1, trades: 3, funding: -0.5, net: -12.5 }]}
      />,
    )).toContain("incl. -$0.50 funding");
  });

  it("says when there was no activity at all", () => {
    expect(text(<DailyPnlTable rows={[]} windowDays={30} />)).toContain(
      "No trades or funding in the last 30 days.",
    );
  });
});
