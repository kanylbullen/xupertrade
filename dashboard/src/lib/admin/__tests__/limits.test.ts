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
  reserveBotStart,
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

/**
 * A tiny model of the database for `reserveBotStart`: one tenant-row
 * lock, held from the FOR UPDATE until the transaction ends (commit or
 * rollback), and a running-bot count that the transaction's SELECT
 * reads and the caller's `reserve` callback bumps. Writes become
 * visible to other transactions only on commit, as under READ
 * COMMITTED.
 */
function installDbModel(initialRunning: number) {
  let committedRunning = initialRunning;
  let lockHeld: Promise<void> = Promise.resolve();
  const order: string[] = [];

  transactionMock.mockImplementation(
    async (cb: (tx: unknown) => Promise<unknown>) => {
      let pendingDelta = 0;
      let release: () => void = () => undefined;
      let locked = false;
      const tx = {
        execute: vi.fn(async () => {
          // FOR UPDATE: wait for the current holder, then hold it
          // ourselves until this transaction ends.
          const prev = lockHeld;
          lockHeld = new Promise<void>((r) => (release = r));
          locked = true;
          await prev;
          order.push("lock");
        }),
        select: vi.fn(() => ({
          from: () => ({
            where: async () => {
              order.push("count");
              return [{ n: committedRunning + pendingDelta }];
            },
          }),
        })),
        // The stale-claim reap. This model has no stale claims; the
        // real statement is exercised in limits.integration.test.ts.
        update: vi.fn(() => ({
          set: () => ({
            where: async () => {
              order.push("reap");
            },
          }),
        })),
        // What a real `reserve` does: make this start countable.
        claim: () => {
          order.push("claim");
          pendingDelta += 1;
        },
      };
      try {
        const out = await cb(tx);
        committedRunning += pendingDelta; // commit
        order.push("commit");
        return out;
      } finally {
        if (locked) release();
      }
    },
  );
  return { order, running: () => committedRunning };
}

type ModelTx = { claim: () => void };

describe("reserveBotStart", () => {
  it("runs reserve in a transaction without lock or count when there is no cap", async () => {
    installDbModel(5);
    const reserve = vi.fn(async (tx: unknown) => {
      (tx as ModelTx).claim();
      return "reserved";
    });
    await expect(
      reserveBotStart({ id: "x", maxActiveBots: null }, reserve),
    ).resolves.toBe("reserved");
    expect(transactionMock).toHaveBeenCalledOnce();
    expect(reserve).toHaveBeenCalledOnce();
  });

  it("locks, counts, then reserves inside the same transaction when below cap", async () => {
    const db = installDbModel(2);
    await reserveBotStart({ id: "x", maxActiveBots: 3 }, async (tx) => {
      (tx as unknown as ModelTx).claim();
    });
    // Stale claims are reaped under the lock, before the count, and
    // this claim lands before commit — i.e. while the lock is held.
    expect(db.order).toEqual(["lock", "reap", "count", "claim", "commit"]);
    expect(db.running()).toBe(3);
  });

  it("throws LimitExceededError at cap and never reserves", async () => {
    const db = installDbModel(3);
    const reserve = vi.fn();
    await expect(
      reserveBotStart({ id: "x", maxActiveBots: 3 }, reserve),
    ).rejects.toBeInstanceOf(LimitExceededError);
    expect(reserve).not.toHaveBeenCalled();
    expect(db.running()).toBe(3);
  });

  it("a cap of 0 — every new tenant's default (NU-8) — refuses the first start", async () => {
    // 0 must mean "no bots", not "falsy, so no cap": the NULL check in
    // reserveBotStart is `!== null`, and this pins that it stays so.
    const db = installDbModel(0);
    const reserve = vi.fn();
    await expect(
      reserveBotStart({ id: "x", maxActiveBots: 0 }, reserve),
    ).rejects.toMatchObject({ kind: "bots_over_cap", current: 0, limit: 0 });
    expect(reserve).not.toHaveBeenCalled();
    expect(db.running()).toBe(0);
  });

  it("lets exactly one of two concurrent starts through a cap of 1", async () => {
    // The finding: the lock used to be released at commit BEFORE
    // anything countable was written (create-and-start set is_running
    // only after Argon2id + the container spawn), so both concurrent
    // callers counted 0 and both passed. Now the second one's count
    // waits for the first one's lock and sees its claim.
    const db = installDbModel(0);
    const reserve = async (tx: unknown) => {
      // Yield inside the transaction, as a real UPDATE/INSERT would,
      // so the two calls genuinely interleave.
      await new Promise((r) => setTimeout(r, 5));
      (tx as ModelTx).claim();
    };

    const results = await Promise.allSettled([
      reserveBotStart({ id: "tid", maxActiveBots: 1 }, reserve),
      reserveBotStart({ id: "tid", maxActiveBots: 1 }, reserve),
    ]);

    const fulfilled = results.filter((r) => r.status === "fulfilled");
    const rejected = results.filter((r) => r.status === "rejected");
    expect(fulfilled).toHaveLength(1);
    expect(rejected).toHaveLength(1);
    expect((rejected[0] as PromiseRejectedResult).reason).toMatchObject({
      kind: "bots_over_cap",
      current: 1,
      limit: 1,
    });
    expect(db.running()).toBe(1);
  });

  it("propagates an error from reserve (e.g. a unique violation)", async () => {
    installDbModel(0);
    const boom = Object.assign(new Error("duplicate"), { code: "23505" });
    await expect(
      reserveBotStart({ id: "x", maxActiveBots: 2 }, async () => {
        throw boom;
      }),
    ).rejects.toBe(boom);
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
