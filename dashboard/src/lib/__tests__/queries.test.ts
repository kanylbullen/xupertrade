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

vi.mock("../db", () => {
  const tradesTable = {
    tenantId: { name: "tenant_id", _: "tenantId" },
    mode: { name: "mode", _: "mode" },
    timestamp: { name: "timestamp", _: "timestamp" },
  };
  return {
    db: {
      select: () => ({
        from: () => ({
          where: (cond: unknown) => {
            whereSpy(cond);
            return {
              orderBy: (...cols: unknown[]) => {
                orderBySpy(...cols);
                return { limit: async () => [] };
              },
            };
          },
        }),
      }),
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
  gte: (col: unknown, val: unknown) => ({ kind: "gte", col, val }),
  sql: Object.assign(
    (strs: TemplateStringsArray, ...vals: unknown[]) => ({ kind: "sql", strs, vals }),
    {},
  ),
  sum: (col: unknown) => ({ kind: "sum", col }),
  count: (col: unknown) => ({ kind: "count", col }),
}));

import { getFirstEquitySince, getRecentTrades } from "../queries";
import { equitySnapshots, trades as tradesTable } from "../db";

beforeEach(() => {
  whereSpy.mockClear();
  orderBySpy.mockClear();
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

  it("is null when the window has no snapshot", async () => {
    expect(await getFirstEquitySince("tenant-1", "paper", new Date())).toBeNull();
  });
});
