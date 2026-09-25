/**
 * Unit tests for queries.ts mode-optional path. Asserts that
 * `getRecentTrades` omits the `mode` predicate when called without a
 * `mode` arg, and includes it when called with one.
 *
 * Mocks the Drizzle chain at the call surface — we're testing what
 * SQL `where()` is constructed, not what postgres returns.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

const whereSpy = vi.fn();
const orderBySpy = vi.fn();
const selectSpy = vi.fn();
// What each query resolves to, in call order; [] once drained.
const results: unknown[][] = [];
const nextRows = () => results.shift() ?? [];

vi.mock("../db", () => {
  const tradesTable = {
    tenantId: { name: "tenant_id", _: "tenantId" },
    mode: { name: "mode", _: "mode" },
    timestamp: { name: "timestamp", _: "timestamp" },
    pnl: { name: "pnl", _: "pnl" },
    fee: { name: "fee", _: "fee" },
    id: { name: "id", _: "id" },
  };
  return {
    db: {
      select: (fields?: unknown) => {
        selectSpy(fields);
        return {
          from: () => ({
            where: (cond: unknown) => {
              whereSpy(cond);
              return {
                orderBy: (...cols: unknown[]) => {
                  orderBySpy(...cols);
                  return { limit: async () => nextRows() };
                },
                groupBy: async () => nextRows(),
                // A query awaited straight after where().
                then: (resolve: (rows: unknown[]) => unknown) => resolve(nextRows()),
              };
            },
          }),
        };
      },
    },
    trades: tradesTable,
    positions: {},
    equitySnapshots: {
      tenantId: { name: "tenant_id", _: "tenantId" },
      mode: { name: "mode", _: "mode" },
      timestamp: { name: "timestamp", _: "timestamp" },
      totalEquity: { name: "total_equity", _: "totalEquity" },
    },
    strategyConfigs: {},
    fundingPayments: {},
  };
});

vi.mock("drizzle-orm", () => ({
  desc: (col: unknown) => ({ kind: "desc", col }),
  eq: (col: unknown, val: unknown) => ({ kind: "eq", col, val }),
  and: (...conds: unknown[]) => ({ kind: "and", conds }),
  gt: (col: unknown, val: unknown) => ({ kind: "gt", col, val }),
  gte: (col: unknown, val: unknown) => ({ kind: "gte", col, val }),
  sql: Object.assign(
    (strs: TemplateStringsArray, ...vals: unknown[]) => ({ kind: "sql", strs, vals }),
    {},
  ),
  sum: (col: unknown) => ({ kind: "sum", col }),
  count: (col: unknown) => ({ kind: "count", col }),
}));

import {
  getDailyPnl,
  getFirstEquitySince,
  getLatestEquity,
  getRealizedPnlTotal,
  getRecentTrades,
} from "../queries";
import { equitySnapshots, trades as tradesTable } from "../db";

beforeEach(() => {
  whereSpy.mockClear();
  orderBySpy.mockClear();
  selectSpy.mockClear();
  results.length = 0;
});

type AndCond = { kind: "and"; conds: Array<{ kind: string; col: unknown; val: unknown }> };

describe("getRecentTrades", () => {
  it("includes a mode predicate when called with a concrete mode", async () => {
    await getRecentTrades("tenant-1", 10, "mainnet");
    expect(whereSpy).toHaveBeenCalledTimes(1);
    const cond = whereSpy.mock.calls[0][0] as AndCond;
    expect(cond.kind).toBe("and");
    const tenantEq = cond.conds.find((c) => c.col === tradesTable.tenantId);
    const modeEq = cond.conds.find((c) => c.col === tradesTable.mode);
    expect(tenantEq?.val).toBe("tenant-1");
    expect(modeEq?.val).toBe("mainnet");
  });

  it("omits the mode predicate when called without a mode (all-modes path)", async () => {
    await getRecentTrades("tenant-1", 10);
    expect(whereSpy).toHaveBeenCalledTimes(1);
    const cond = whereSpy.mock.calls[0][0] as AndCond;
    expect(cond.kind).toBe("and");
    expect(cond.conds).toHaveLength(1);
    expect(cond.conds[0].col).toBe(tradesTable.tenantId);
    expect(cond.conds[0].val).toBe("tenant-1");
  });

  it("treats undefined explicitly as the all-modes case", async () => {
    await getRecentTrades("tenant-1", 10, undefined);
    const cond = whereSpy.mock.calls[0][0] as AndCond;
    expect(cond.conds).toHaveLength(1);
  });
});

describe("getFirstEquitySince", () => {
  it("takes the earliest of this tenant's snapshots in the mode at or after `since`", async () => {
    const since = new Date("2026-09-24T12:00:00Z");
    await getFirstEquitySince("tenant-1", "testnet", since);
    const cond = whereSpy.mock.calls[0][0] as AndCond;
    const byCol = (col: unknown) => cond.conds.find((c) => c.col === col);
    expect(byCol(equitySnapshots.tenantId)).toMatchObject({ kind: "eq", val: "tenant-1" });
    expect(byCol(equitySnapshots.mode)).toMatchObject({ kind: "eq", val: "testnet" });
    expect(byCol(equitySnapshots.timestamp)).toMatchObject({ kind: "gte", val: since });
    // A bare column is ascending in Drizzle: the first snapshot, not the latest.
    expect(orderBySpy).toHaveBeenCalledWith(equitySnapshots.timestamp);
  });

  it("skips $0 snapshots, which are failed balance reads", async () => {
    await getFirstEquitySince("tenant-1", "testnet", new Date());
    const cond = whereSpy.mock.calls[0][0] as AndCond;
    expect(cond.conds.find((c) => c.col === equitySnapshots.totalEquity)).toMatchObject({
      kind: "gt",
      val: 0,
    });
  });

  it("is null when the window has no snapshot", async () => {
    expect(await getFirstEquitySince("tenant-1", "paper", new Date())).toBeNull();
  });
});

describe("getLatestEquity", () => {
  it("skips $0 snapshots too", async () => {
    await getLatestEquity("tenant-1", "testnet");
    const cond = whereSpy.mock.calls[0][0] as AndCond;
    expect(cond.conds.find((c) => c.col === equitySnapshots.totalEquity)).toMatchObject({
      kind: "gt",
      val: 0,
    });
  });
});

type SqlFragment = { kind: "sql"; strs: TemplateStringsArray; vals: unknown[] };

describe("entry fees", () => {
  it("are the fees on rows with no pnl", async () => {
    await getRealizedPnlTotal("tenant-1", "testnet");
    const fields = selectSpy.mock.calls[0][0] as { entryFees: SqlFragment };
    const sqlText = fields.entryFees.strs.join("?");
    expect(sqlText).toMatch(/sum\(\?\) filter \(where \? is null\)/);
    expect(fields.entryFees.vals).toEqual([tradesTable.fee, tradesTable.pnl]);
  });

  it("come back with the realized total", async () => {
    results.push([{ realizedPnl: "-0.45", fees: "0.9", entryFees: "0.45", trades: 2 }]);
    expect(await getRealizedPnlTotal("tenant-1", "testnet")).toEqual({
      realizedPnl: -0.45,
      fees: 0.9,
      entryFees: 0.45,
      trades: 2,
    });
  });

  it("come off each day's net, with funding added", async () => {
    // One flat $1,000 round trip on the 24th (0.45 fee each side, the
    // close's pnl -0.45), funding on both days, an open on the 25th.
    results.push(
      [
        { date: "2026-09-24", realizedPnl: "-0.45", fees: "0.9", entryFees: "0.45", trades: 2 },
        { date: "2026-09-25", realizedPnl: "0", fees: "0.5", entryFees: "0.5", trades: 1 },
      ],
      [
        { date: "2026-09-24", funding: "-0.1" },
        { date: "2026-09-26", funding: "0.2" },
      ],
    );
    const rows = await getDailyPnl("tenant-1", "testnet", 30);
    const net = Object.fromEntries(rows.map((r) => [r.date, r.net]));
    expect(net["2026-09-24"]).toBeCloseTo(-1.0, 10);
    expect(net["2026-09-25"]).toBeCloseTo(-0.5, 10);
    expect(net["2026-09-26"]).toBeCloseTo(0.2, 10);
  });
});
