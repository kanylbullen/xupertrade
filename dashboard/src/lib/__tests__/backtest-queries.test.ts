/**
 * SQL-shape tests for the Backtests-page queries (getBacktestRunsPage /
 * countBacktestRuns / getBacktestStrategyNames / getBacktestTrend).
 *
 * Same style as trades-page-queries.test.ts: mocks the Drizzle chain at
 * the call surface — we assert which predicates get built and how the
 * page window is derived, not what postgres returns. The tenant
 * predicate is checked explicitly on every path: the dashboard connects
 * as superuser, so RLS is not enforced and a dropped `tenant_id`
 * clause would leak across tenants.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

const whereSpy = vi.fn();
const limitSpy = vi.fn();
const offsetSpy = vi.fn();
const orderBySpy = vi.fn();

// Mutable terminal value for the mocked chain's `then` — lets each
// test control what the query resolves to (count row, names, runs…).
const mockState = vi.hoisted(() => ({ resolved: [] as unknown }));

vi.mock("../db", () => {
  const backtestRunsTable = {
    tenantId: { name: "tenant_id" },
    strategyName: { name: "strategy_name" },
    createdAt: { name: "created_at" },
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
    then: (resolve: (v: unknown) => unknown) => resolve(mockState.resolved),
  };
  return {
    db: {
      select: () => chain,
      selectDistinct: () => chain,
    },
    trades: {},
    positions: {},
    equitySnapshots: {},
    strategyConfigs: {},
    fundingPayments: {},
    backtestRuns: backtestRunsTable,
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

import {
  countBacktestRuns,
  getBacktestRunsPage,
  getBacktestStrategyNames,
  getBacktestTrend,
} from "../queries";
import { backtestRuns as b } from "../db";

type Cond = { kind: string; col?: unknown; val?: unknown };
type AndCond = { kind: "and"; conds: Cond[] };

beforeEach(() => {
  whereSpy.mockClear();
  limitSpy.mockClear();
  offsetSpy.mockClear();
  orderBySpy.mockClear();
  mockState.resolved = [];
});

function conds(): Cond[] {
  return (whereSpy.mock.calls[0][0] as AndCond).conds;
}

describe("getBacktestRunsPage", () => {
  it("always constrains by tenant", async () => {
    await getBacktestRunsPage("tenant-1", {}, 50, 0);
    const tenant = conds().find((c) => c.col === b.tenantId);
    expect(tenant?.val).toBe("tenant-1");
  });

  it("adds only the filters that were supplied", async () => {
    await getBacktestRunsPage("tenant-1", {}, 50, 0);
    // tenant only — no strategy/date predicates
    expect(conds()).toHaveLength(1);
  });

  it("builds strategy and both date bounds when given", async () => {
    const from = new Date("2026-08-01T00:00:00.000Z");
    const to = new Date("2026-08-02T00:00:00.000Z");
    await getBacktestRunsPage(
      "tenant-1",
      { strategy: "bb_short", from, to },
      50,
      0,
    );
    const c = conds();
    expect(c.find((x) => x.col === b.strategyName)?.val).toBe("bb_short");
    const gte = c.find((x) => x.kind === "gte");
    const lt = c.find((x) => x.kind === "lt");
    expect(gte?.val).toBe(from);
    // End bound must be exclusive (`lt`), matching exclusiveEnd()'s
    // +1 day. A `lte` here would double-count the boundary day.
    expect(lt?.val).toBe(to);
  });

  it("orders by created_at then id so paging is stable", async () => {
    // A `--all` CLI sweep saves one row per strategy in the same
    // second; without the unique id tiebreaker, OFFSET paging can
    // skip or repeat rows.
    await getBacktestRunsPage("tenant-1", {}, 50, 0);
    const cols = orderBySpy.mock.calls[0][0] as Array<{ col: unknown }>;
    expect(cols).toHaveLength(2);
    expect(cols[0].col).toBe(b.createdAt);
    expect(cols[1].col).toBe(b.id);
  });

  it("passes the page window through as limit/offset", async () => {
    await getBacktestRunsPage("tenant-1", {}, 50, 100);
    expect(limitSpy).toHaveBeenCalledWith(50);
    expect(offsetSpy).toHaveBeenCalledWith(100);
  });
});

describe("countBacktestRuns", () => {
  it("counts under the same predicates as the page query", async () => {
    const from = new Date("2026-08-01T00:00:00.000Z");
    await getBacktestRunsPage("tenant-1", { strategy: "bb_short", from }, 50, 0);
    const pageConds = conds();
    whereSpy.mockClear();

    await countBacktestRuns("tenant-1", { strategy: "bb_short", from });
    const countConds = conds();

    // Same shape — a divergence here would show a total that doesn't
    // match the rows on screen.
    expect(countConds).toHaveLength(pageConds.length);
    expect(countConds.find((c) => c.col === b.strategyName)?.val).toBe(
      "bb_short",
    );
    expect(countConds.find((c) => c.kind === "gte")?.val).toBe(from);
  });

  it("returns the scalar count", async () => {
    mockState.resolved = [{ n: 37 }];
    await expect(countBacktestRuns("tenant-1", {})).resolves.toBe(37);
  });
});

describe("getBacktestStrategyNames", () => {
  it("filters by tenant and resolves the distinct names", async () => {
    mockState.resolved = [{ name: "bb_short" }, { name: "ema_crossover" }];
    await expect(getBacktestStrategyNames("tenant-1")).resolves.toEqual([
      "bb_short",
      "ema_crossover",
    ]);
    // Bare eq() — no and() wrapper — is passed straight to where().
    const where = whereSpy.mock.calls[0][0] as Cond;
    expect(where.col).toBe(b.tenantId);
    expect(where.val).toBe("tenant-1");
  });
});

describe("getBacktestTrend", () => {
  it("constrains by tenant AND strategy", async () => {
    mockState.resolved = [];
    await getBacktestTrend("tenant-1", "bb_short");
    const c = conds();
    expect(c.find((x) => x.col === b.tenantId)?.val).toBe("tenant-1");
    expect(c.find((x) => x.col === b.strategyName)?.val).toBe("bb_short");
  });

  it("queries newest-first with the id tiebreaker and applies the cap", async () => {
    mockState.resolved = [];
    await getBacktestTrend("tenant-1", "bb_short", 200);
    const cols = orderBySpy.mock.calls[0][0] as Array<{ col: unknown }>;
    expect(cols).toHaveLength(2);
    expect(cols[0].col).toBe(b.createdAt);
    expect(cols[1].col).toBe(b.id);
    expect(limitSpy).toHaveBeenCalledWith(200);
  });

  it("reverses the rows to oldest-first for charting", async () => {
    // DB returns newest-first; recharts wants left-to-right time order.
    mockState.resolved = [{ id: 3 }, { id: 1 }, { id: 2 }];
    await expect(getBacktestTrend("tenant-1", "bb_short")).resolves.toEqual([
      { id: 2 },
      { id: 1 },
      { id: 3 },
    ]);
  });
});
