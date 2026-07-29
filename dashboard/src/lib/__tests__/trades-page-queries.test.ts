/**
 * SQL-shape tests for the filtered/paginated Trades queries.
 *
 * Mocks the Drizzle chain at the call surface — we assert which
 * predicates get built and how the page window is derived, not what
 * postgres returns. The tenant predicate is checked explicitly on
 * every path: the dashboard connects as superuser, so RLS is not
 * enforced and a dropped `tenant_id` clause would leak across tenants.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

const whereSpy = vi.fn();
const limitSpy = vi.fn();
const offsetSpy = vi.fn();
const orderBySpy = vi.fn();

vi.mock("../db", () => {
  const tradesTable = {
    tenantId: { name: "tenant_id" },
    mode: { name: "mode" },
    strategyName: { name: "strategy_name" },
    timestamp: { name: "timestamp" },
    id: { name: "id" },
  };
  const chain = {
    from: () => chain,
    where: (c: unknown) => {
      whereSpy(c);
      return chain;
    },
    orderBy: (...cols: unknown[]) => {
      orderBySpy(cols);
      return chain;
    },
    limit: (n: number) => {
      limitSpy(n);
      return chain;
    },
    offset: async (n: number) => {
      offsetSpy(n);
      return [];
    },
    then: (resolve: (v: unknown) => unknown) => resolve([{ n: 7 }]),
  };
  return {
    db: {
      select: () => chain,
      selectDistinct: () => chain,
    },
    trades: tradesTable,
    positions: {},
    equitySnapshots: {},
    strategyConfigs: {},
    fundingPayments: {},
  };
});

vi.mock("drizzle-orm", () => ({
  desc: (col: unknown) => ({ kind: "desc", col }),
  eq: (col: unknown, val: unknown) => ({ kind: "eq", col, val }),
  and: (...conds: unknown[]) => ({ kind: "and", conds }),
  gte: (col: unknown, val: unknown) => ({ kind: "gte", col, val }),
  lt: (col: unknown, val: unknown) => ({ kind: "lt", col, val }),
  sql: Object.assign(() => ({ kind: "sql" }), {}),
  sum: (col: unknown) => ({ kind: "sum", col }),
  count: () => ({ kind: "count" }),
}));

import { countTrades, getTradesPage } from "../queries";
import { trades as t } from "../db";

type Cond = { kind: string; col?: unknown; val?: unknown };
type AndCond = { kind: "and"; conds: Cond[] };

beforeEach(() => {
  whereSpy.mockClear();
  limitSpy.mockClear();
  offsetSpy.mockClear();
  orderBySpy.mockClear();
});

function conds(): Cond[] {
  return (whereSpy.mock.calls[0][0] as AndCond).conds;
}

describe("getTradesPage", () => {
  it("always constrains by tenant", async () => {
    await getTradesPage("tenant-1", {}, 50, 0);
    const tenant = conds().find((c) => c.col === t.tenantId);
    expect(tenant?.val).toBe("tenant-1");
  });

  it("adds only the filters that were supplied", async () => {
    await getTradesPage("tenant-1", {}, 50, 0);
    // tenant only — no mode/strategy/date predicates
    expect(conds()).toHaveLength(1);
  });

  it("builds mode, strategy and both date bounds when given", async () => {
    const from = new Date("2026-07-01T00:00:00.000Z");
    const to = new Date("2026-07-02T00:00:00.000Z");
    await getTradesPage(
      "tenant-1",
      { mode: "mainnet", strategy: "bb_short", from, to },
      50,
      0,
    );
    const c = conds();
    expect(c.find((x) => x.col === t.mode)?.val).toBe("mainnet");
    expect(c.find((x) => x.col === t.strategyName)?.val).toBe("bb_short");
    const gte = c.find((x) => x.kind === "gte");
    const lt = c.find((x) => x.kind === "lt");
    expect(gte?.val).toBe(from);
    // End bound must be exclusive (`lt`), matching exclusiveEnd()'s
    // +1 day. A `lte` here would double-count the boundary day.
    expect(lt?.val).toBe(to);
  });

  it("orders by timestamp then id so paging is stable", async () => {
    // Trades written in the same tick share a timestamp; without the
    // unique id tiebreaker, OFFSET paging can skip or repeat rows.
    await getTradesPage("tenant-1", {}, 50, 0);
    const cols = orderBySpy.mock.calls[0][0] as Array<{ col: unknown }>;
    expect(cols).toHaveLength(2);
    expect(cols[0].col).toBe(t.timestamp);
    expect(cols[1].col).toBe(t.id);
  });

  it("passes the page window through as limit/offset", async () => {
    await getTradesPage("tenant-1", {}, 50, 100);
    expect(limitSpy).toHaveBeenCalledWith(50);
    expect(offsetSpy).toHaveBeenCalledWith(100);
  });
});

describe("countTrades", () => {
  it("counts under the same predicates as the page query", async () => {
    const from = new Date("2026-07-01T00:00:00.000Z");
    await getTradesPage("tenant-1", { mode: "paper", from }, 50, 0);
    const pageConds = conds();
    whereSpy.mockClear();

    await countTrades("tenant-1", { mode: "paper", from });
    const countConds = conds();

    // Same shape — a divergence here would show a total that doesn't
    // match the rows on screen.
    expect(countConds).toHaveLength(pageConds.length);
    expect(countConds.find((c) => c.col === t.mode)?.val).toBe("paper");
    expect(countConds.find((c) => c.kind === "gte")?.val).toBe(from);
  });

  it("returns the scalar count", async () => {
    await expect(countTrades("tenant-1", {})).resolves.toBe(7);
  });
});
