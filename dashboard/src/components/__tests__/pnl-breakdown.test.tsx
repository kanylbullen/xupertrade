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
  const base = { entryFees: 0, funding: 0, unrealized: 0, dbConnected: true };

  it("shows every negative amount with its minus", () => {
    const out = text(
      <PnlSummary
        {...base}
        realized={-42.1}
        entryFees={3.5}
        funding={-1.25}
        unrealized={-7}
      />,
    );
    expect(out).toContain("Realized (after close fees) -$42.10");
    expect(out).toContain("Unrealized (open positions) -$7.00");
    expect(out).toContain("Funding (cumulative) -$1.25");
    expect(out).toContain("Entry fees (not in realized) -$3.50");
    expect(out).toContain("Net P&L -$46.85");
    expect(out).toContain("Total inc. unrealized -$53.85");
    expect(out).not.toMatch(/\$-/);
  });

  it("takes entry fees off net: a close's pnl carries only its own fee", () => {
    // $1,000 in and out at a flat price, 0.045% taker each side: the
    // open row has pnl NULL and fee 0.45, the close pnl -0.45 and fee
    // 0.45. Net used to read -$0.45 under a "Fees (in realized) -$0.90"
    // row; the true net is -$0.90.
    const out = text(<PnlSummary {...base} realized={-0.45} entryFees={0.45} />);
    expect(out).toContain("Realized (after close fees) -$0.45");
    expect(out).toContain("Entry fees (not in realized) -$0.45");
    expect(out).toContain("Net P&L -$0.90");
    expect(out).not.toContain("Fees (in realized)");
  });

  it("shows a net fee rebate as a gain, not as fees paid", () => {
    const out = text(<PnlSummary {...base} realized={10} entryFees={-0.4} />);
    expect(out).toContain("Entry fees (not in realized) +$0.40");
    expect(out).toContain("Net P&L +$10.40");
  });

  it("marks gains with a plus", () => {
    const out = text(<PnlSummary {...base} realized={5} funding={2} />);
    expect(out).toContain("Net P&L +$7.00");
  });

  it("shows no figures when the database is unreachable", () => {
    const out = text(<PnlSummary {...base} realized={0} dbConnected={false} />);
    expect(out).toContain("P&L breakdown (all-time) DB offline");
    expect(out).not.toContain("$0.00");
  });
});

describe("StrategyPnlTable", () => {
  it("signs realized P&L and shows fees as a cost", () => {
    const out = text(
      <StrategyPnlTable
        dbConnected
        rows={[
          { strategyName: "loser", trades: 4, wins: 1, losses: 3, realizedPnl: -18.4, fees: 2.2 },
          { strategyName: "winner", trades: 2, wins: 2, losses: 0, realizedPnl: 9, fees: 0.8 },
        ]}
      />,
    );
    expect(out).toContain("loser 4 1 / 3 25% -$2.20 -$18.40");
    expect(out).toContain("winner 2 2 / 0 100% -$0.80 +$9.00");
  });

  it("does not claim there are no trades when the database is unreachable", () => {
    const out = text(<StrategyPnlTable rows={[]} dbConnected={false} />);
    expect(out).toContain("DB offline");
    expect(out).not.toContain("No closed trades");
  });
});

describe("DailyPnlTable", () => {
  const day = {
    date: "2026-09-24",
    realizedPnl: -12,
    fees: 1,
    entryFees: 0.4,
    trades: 3,
    funding: -0.5,
    net: -12.9,
  };

  it("signs each day's net and states the window", () => {
    const out = text(
      <DailyPnlTable
        dbConnected
        windowDays={30}
        rows={[
          day,
          { date: "2026-09-25", realizedPnl: 4, fees: 0.2, entryFees: 0, trades: 1, funding: 0, net: 4 },
        ]}
      />,
    );
    expect(out).toContain("2 days with activity in the last 30 days");
    expect(out).toContain("Net = realized − entry fees + funding");
    expect(out).toContain("2026-09-24 3t -$12.90");
    expect(out).toContain("2026-09-25 1t +$4.00");
    expect(
      renderToStaticMarkup(<DailyPnlTable dbConnected windowDays={30} rows={[day]} />),
    ).toContain("incl. -$0.40 entry fees, -$0.50 funding");
  });

  it("says when there was no activity at all", () => {
    expect(text(<DailyPnlTable dbConnected rows={[]} windowDays={30} />)).toContain(
      "No trades or funding in the last 30 days.",
    );
  });

  it("does not claim a quiet month when the database is unreachable", () => {
    const out = text(<DailyPnlTable dbConnected={false} rows={[]} windowDays={30} />);
    expect(out).toContain("DB offline");
    expect(out).not.toContain("No trades");
  });
});
