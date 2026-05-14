import { describe, expect, it, vi, beforeEach } from "vitest";

// Mock db so we don't need a live postgres connection. vi.mock is
// hoisted above imports — use vi.hoisted to keep the mock fn in scope.
const { selectMock } = vi.hoisted(() => ({ selectMock: vi.fn() }));
vi.mock("@/lib/db", () => {
  return {
    db: { select: selectMock },
    tenantBots: {} as unknown,
    tenants: {} as unknown,
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
});

function mockBotCount(n: number) {
  selectMock.mockReturnValueOnce({
    from: () => ({
      where: () => Promise.resolve([{ n }]),
    }),
  });
}

describe("assertCanStartBot", () => {
  it("is a no-op when maxActiveBots is null", async () => {
    await expect(
      assertCanStartBot({ id: "x", maxActiveBots: null }),
    ).resolves.toBeUndefined();
    expect(selectMock).not.toHaveBeenCalled();
  });

  it("passes when running bots are below cap", async () => {
    mockBotCount(2);
    await expect(
      assertCanStartBot({ id: "x", maxActiveBots: 3 }),
    ).resolves.toBeUndefined();
  });

  it("throws LimitExceededError at cap", async () => {
    mockBotCount(3);
    await expect(
      assertCanStartBot({ id: "x", maxActiveBots: 3 }),
    ).rejects.toBeInstanceOf(LimitExceededError);
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
