/**
 * Tests for lib/services-owner-check.ts (roadmap NU-7): the warning the
 * start and stop routes log when a tenant has no running services owner.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("drizzle-orm", () => ({
  eq: (col: unknown, val: unknown) => ({ kind: "eq", col, val }),
  and: (...conds: unknown[]) => ({ kind: "and", conds }),
}));

const chain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
vi.mock("../db", () => ({
  db: { select: vi.fn(() => chain) },
  tenantBots: { id: "id", tenantId: "tenantId", mode: "mode", isRunning: "isRunning" },
}));

import { servicesOwnerRunning, warnIfServicesOwnerNotRunning } from "../services-owner-check";

const ORIG_ENV = { ...process.env };

beforeEach(() => {
  delete process.env.HYPERTRADE_SERVICES_OWNER_MODE;
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.clearAllMocks();
  chain.limit.mockReset();
  vi.mocked(console.warn).mockRestore();
});

describe("servicesOwnerRunning", () => {
  it("asks for this tenant's running bot in the owner mode", async () => {
    process.env.HYPERTRADE_SERVICES_OWNER_MODE = "mainnet";
    chain.limit.mockResolvedValueOnce([{ id: "b1" }]);
    expect(await servicesOwnerRunning("t1")).toBe(true);
    expect(chain.where).toHaveBeenCalledWith({
      kind: "and",
      conds: [
        { kind: "eq", col: "tenantId", val: "t1" },
        { kind: "eq", col: "mode", val: "mainnet" },
        { kind: "eq", col: "isRunning", val: true },
      ],
    });
  });

  it("is false when no such row exists", async () => {
    chain.limit.mockResolvedValueOnce([]);
    expect(await servicesOwnerRunning("t1")).toBe(false);
  });
});

describe("warnIfServicesOwnerNotRunning", () => {
  it("stays quiet when the owner runs", async () => {
    chain.limit.mockResolvedValueOnce([{ id: "b1" }]);
    await warnIfServicesOwnerNotRunning("t1", "after stopping the testnet bot");
    expect(console.warn).not.toHaveBeenCalled();
  });

  it("warns with context when the owner is not running", async () => {
    chain.limit.mockResolvedValueOnce([]);
    await warnIfServicesOwnerNotRunning("t1", "after stopping the paper bot");
    const line = vi.mocked(console.warn).mock.calls[0][0] as string;
    expect(line).toContain("after stopping the paper bot");
    expect(line).toContain("no running paper bot for tenant t1");
  });

  it("never throws when the lookup fails", async () => {
    chain.limit.mockRejectedValueOnce(new Error("db down"));
    await expect(
      warnIfServicesOwnerNotRunning("t1", "after starting the testnet bot"),
    ).resolves.toBeUndefined();
    expect(vi.mocked(console.warn).mock.calls[0][0]).toContain("owner check failed");
  });
});
