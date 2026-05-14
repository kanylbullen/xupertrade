import { describe, expect, it, vi, beforeEach } from "vitest";

// Mock db so we don't need a live postgres connection. vi.mock is
// hoisted above imports — use vi.hoisted to keep the mock fns in scope.
const { selectMock, transactionMock, executeMock, txSelectMock } = vi.hoisted(
  () => ({
    selectMock: vi.fn(),
    transactionMock: vi.fn(),
    executeMock: vi.fn(),
    txSelectMock: vi.fn(),
  }),
);
vi.mock("@/lib/db", () => {
  return {
    db: { select: selectMock, transaction: transactionMock },
    tenantBots: {} as unknown,
    tenants: { id: "id" } as unknown,
  };
});

import {
  LimitExceededError,
  assertCanEnableStrategy,
  assertCanStartBot,
  assertStrategyAllowed,
  computeLimitsWarnings,
} from "../limits";

beforeEach(() => {
  selectMock.mockReset();
  transactionMock.mockReset();
  executeMock.mockReset();
  txSelectMock.mockReset();
});

function mockBotCount(n: number) {
  selectMock.mockReturnValueOnce({
    from: () => ({
      where: () => Promise.resolve([{ n }]),
    }),
  });
}

// For assertCanStartBot — mocks db.transaction(cb) and the tx.execute
// + tx.select(...).from().where() chain that runs inside it. Each
// call queues one bot-count to return.
function mockTxBotCount(n: number) {
  transactionMock.mockImplementationOnce(async (cb: (tx: unknown) => unknown) => {
    const tx = {
      execute: vi.fn().mockResolvedValue(undefined),
      select: vi.fn().mockReturnValue({
        from: () => ({
          where: () => Promise.resolve([{ n }]),
        }),
      }),
    };
    return cb(tx);
  });
}

describe("assertCanStartBot", () => {
  it("is a no-op when maxActiveBots is null", async () => {
    await expect(
      assertCanStartBot({ id: "x", maxActiveBots: null }),
    ).resolves.toBeUndefined();
    expect(transactionMock).not.toHaveBeenCalled();
  });

  it("passes when running bots are below cap", async () => {
    mockTxBotCount(2);
    await expect(
      assertCanStartBot({ id: "x", maxActiveBots: 3 }),
    ).resolves.toBeUndefined();
  });

  it("throws LimitExceededError at cap", async () => {
    mockTxBotCount(3);
    await expect(
      assertCanStartBot({ id: "x", maxActiveBots: 3 }),
    ).rejects.toBeInstanceOf(LimitExceededError);
  });

  it("serializes concurrent calls for the same tenant at-cap-minus-one", async () => {
    // Simulate the FOR UPDATE lock: the second transaction's callback
    // doesn't start running until the first one has completed (which
    // is how `SELECT ... FOR UPDATE` behaves at the DB level). After
    // the first call passes, the bot count seen by the second call is
    // one higher, so the cap check trips.
    let firstDone = false;
    let observedCount = 2;
    transactionMock.mockImplementation(async (cb: (tx: unknown) => unknown) => {
      // Lock: wait until the previous in-flight tx (if any) finished.
      while (transactionMock.mock.calls.length > 1 && !firstDone) {
        await new Promise((r) => setTimeout(r, 1));
      }
      const tx = {
        execute: vi.fn().mockResolvedValue(undefined),
        select: vi.fn().mockReturnValue({
          from: () => ({
            where: () => Promise.resolve([{ n: observedCount }]),
          }),
        }),
      };
      try {
        const r = await cb(tx);
        // First call passed — bump the simulated running count so a
        // racing second call sees the new state.
        observedCount += 1;
        return r;
      } finally {
        firstDone = true;
      }
    });

    const results = await Promise.allSettled([
      assertCanStartBot({ id: "tid", maxActiveBots: 3 }),
      assertCanStartBot({ id: "tid", maxActiveBots: 3 }),
    ]);
    const fulfilled = results.filter((r) => r.status === "fulfilled");
    const rejected = results.filter((r) => r.status === "rejected");
    expect(fulfilled).toHaveLength(1);
    expect(rejected).toHaveLength(1);
    expect((rejected[0] as PromiseRejectedResult).reason).toBeInstanceOf(
      LimitExceededError,
    );
  });
});

describe("assertStrategyAllowed", () => {
  it("is a no-op when allowedStrategies is null", () => {
    expect(() => assertStrategyAllowed({ allowedStrategies: null }, "foo")).not.toThrow();
  });
  it("passes when strategy is in allowlist", () => {
    expect(() =>
      assertStrategyAllowed({ allowedStrategies: ["foo", "bar"] }, "foo"),
    ).not.toThrow();
  });
  it("throws when strategy is not in allowlist", () => {
    expect(() =>
      assertStrategyAllowed({ allowedStrategies: ["bar"] }, "foo"),
    ).toThrowError(LimitExceededError);
  });
  it("treats empty allowlist as deny-all", () => {
    expect(() => assertStrategyAllowed({ allowedStrategies: [] }, "foo")).toThrow();
  });
});

describe("assertCanEnableStrategy", () => {
  it("is a no-op when cap is null", () => {
    expect(() => assertCanEnableStrategy({ maxActiveStrategies: null }, 99)).not.toThrow();
  });
  it("passes below cap", () => {
    expect(() => assertCanEnableStrategy({ maxActiveStrategies: 5 }, 4)).not.toThrow();
  });
  it("throws at cap", () => {
    expect(() => assertCanEnableStrategy({ maxActiveStrategies: 5 }, 5)).toThrow();
  });
});

describe("computeLimitsWarnings", () => {
  it("returns empty when nothing is over cap", async () => {
    mockBotCount(1);
    const w = await computeLimitsWarnings(
      "tid",
      { maxActiveBots: 3, maxActiveStrategies: 5, allowedStrategies: null },
      ["a", "b"],
    );
    expect(w).toEqual([]);
  });

  it("flags bots over cap", async () => {
    mockBotCount(5);
    const w = await computeLimitsWarnings(
      "tid",
      { maxActiveBots: 2, maxActiveStrategies: null, allowedStrategies: null },
      [],
    );
    expect(w).toContainEqual({ kind: "bots_over_cap", current: 5, limit: 2 });
  });

  it("flags active strategies outside allowlist", async () => {
    const w = await computeLimitsWarnings(
      "tid",
      {
        maxActiveBots: null,
        maxActiveStrategies: null,
        allowedStrategies: ["allowed_one"],
      },
      ["allowed_one", "blocked_two"],
    );
    expect(w).toContainEqual({
      kind: "active_strategies_outside_allowlist",
      names: ["blocked_two"],
    });
  });

  it("flags strategies over cap", async () => {
    const w = await computeLimitsWarnings(
      "tid",
      { maxActiveBots: null, maxActiveStrategies: 1, allowedStrategies: null },
      ["a", "b", "c"],
    );
    expect(w).toContainEqual({ kind: "strategies_over_cap", current: 3, limit: 1 });
  });
});
