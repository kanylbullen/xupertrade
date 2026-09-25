/**
 * Tests for lib/services-owner-check.ts (roadmap NU-7): the owner-bot
 * lookup /hodl and /vaults use, the running-owner lookup read from the
 * containers' labels, and the warning the start and stop routes log.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("drizzle-orm", () => ({
  eq: (col: unknown, val: unknown) => ({ kind: "eq", col, val }),
  and: (...conds: unknown[]) => ({ kind: "and", conds }),
}));

const chain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn(),
  limit: vi.fn(),
};
vi.mock("../db", () => ({
  db: { select: vi.fn(() => chain) },
  tenantBots: { id: "id", tenantId: "tenantId", mode: "mode", isRunning: "isRunning" },
}));
vi.mock("../bot-orchestrator", () => ({ statusBot: vi.fn() }));

import { statusBot } from "../bot-orchestrator";
import {
  ownerBotRow,
  runningServicesOwners,
  servicesOwnerProblem,
  warnIfServicesOwnerNotRunning,
} from "../services-owner-check";

const mockedStatus = vi.mocked(statusBot);
const OPERATOR = { id: "t1", isOperator: true };
const ORIG_ENV = { ...process.env };

/** A running row plus the labels its container was started with. */
function running(mode: string, owner: string | undefined) {
  const labels: Record<string, string> = { "hypertrade.mode": mode };
  if (owner !== undefined) labels["hypertrade.services_owner"] = owner;
  return { row: { id: `b-${mode}`, mode, containerId: `c-${mode}` }, labels };
}

function givenRunning(...bots: ReturnType<typeof running>[]) {
  chain.where.mockResolvedValueOnce(bots.map((b) => b.row));
  for (const b of bots) {
    mockedStatus.mockResolvedValueOnce({ labels: b.labels } as never);
  }
}

beforeEach(() => {
  delete process.env.HYPERTRADE_SERVICES_OWNER_MODE;
  chain.where.mockReturnValue(chain);
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.clearAllMocks();
  chain.where.mockReset();
  chain.limit.mockReset();
  mockedStatus.mockReset();
  vi.mocked(console.warn).mockRestore();
});

describe("ownerBotRow", () => {
  it.each([
    [{ id: "t1", isOperator: true }, undefined, "paper"],
    [{ id: "t1", isOperator: true }, "testnet", "testnet"],
    [{ id: "t1", isOperator: false }, "paper", "mainnet"],
  ])("asks for the tenant's owner-mode row (%j, env %s → %s)", async (tenant, env, mode) => {
    if (env) process.env.HYPERTRADE_SERVICES_OWNER_MODE = env;
    chain.limit.mockResolvedValueOnce([{ id: "b1" }]);
    expect(await ownerBotRow(tenant)).toEqual({ id: "b1" });
    expect(chain.where).toHaveBeenCalledWith({
      kind: "and",
      conds: [
        { kind: "eq", col: "tenantId", val: "t1" },
        { kind: "eq", col: "mode", val: mode },
      ],
    });
  });

  it("is null when the tenant has no such bot", async () => {
    chain.limit.mockResolvedValueOnce([]);
    expect(await ownerBotRow(OPERATOR)).toBeNull();
  });
});

describe("runningServicesOwners", () => {
  it("keeps the running bots whose container was started as owner", async () => {
    givenRunning(running("paper", "true"), running("testnet", "false"), running("mainnet", "false"));
    const owners = await runningServicesOwners("t1");
    expect(owners.map((o) => o.mode)).toEqual(["paper"]);
    expect(chain.where).toHaveBeenCalledWith({
      kind: "and",
      conds: [
        { kind: "eq", col: "tenantId", val: "t1" },
        { kind: "eq", col: "isRunning", val: true },
      ],
    });
  });

  it("counts a pre-NU-7 mainnet container, which still has Telegram on", async () => {
    givenRunning(running("paper", undefined), running("mainnet", undefined));
    expect((await runningServicesOwners("t1")).map((o) => o.mode)).toEqual(["mainnet"]);
  });

  it("skips claims and removed containers", async () => {
    chain.where.mockResolvedValueOnce([
      { id: "b1", mode: "paper", containerId: "claiming" },
      { id: "b2", mode: "testnet", containerId: null },
      { id: "b3", mode: "mainnet", containerId: "gone" },
    ]);
    mockedStatus.mockResolvedValueOnce(null);
    expect(await runningServicesOwners("t1")).toEqual([]);
    expect(mockedStatus).toHaveBeenCalledOnce();
  });
});

describe("servicesOwnerProblem", () => {
  it.each([
    [["paper"], null],
    [[], "has none: Telegram, HODL"],
    [["mainnet", "paper"], "has mainnet + paper: their Telegram pollers collide"],
    [["mainnet"], "has mainnet: the old owner still runs them"],
  ])("owners %j → %j", (modes, text) => {
    const problem = servicesOwnerProblem("t1", "paper", modes);
    if (text === null) expect(problem).toBeNull();
    else expect(problem).toContain(text);
  });
});

describe("warnIfServicesOwnerNotRunning", () => {
  it("stays quiet when the owner runs", async () => {
    givenRunning(running("paper", "true"), running("testnet", "false"));
    await warnIfServicesOwnerNotRunning(OPERATOR, "after stopping the testnet bot");
    expect(console.warn).not.toHaveBeenCalled();
  });

  it("warns when the paper row runs but was started before the owner moved", async () => {
    // First deploy: the old mainnet owner was restarted first and came
    // back without Telegram; paper still runs with its old env.
    givenRunning(running("paper", undefined), running("mainnet", "false"));
    await warnIfServicesOwnerNotRunning(OPERATOR, "after starting the mainnet bot");
    const line = vi.mocked(console.warn).mock.calls[0][0] as string;
    expect(line).toContain("after starting the mainnet bot");
    expect(line).toContain("tenant t1 should have one running side-services owner");
    expect(line).toContain("has none");
  });

  it("never throws when the lookup fails", async () => {
    chain.where.mockRejectedValueOnce(new Error("db down"));
    await expect(
      warnIfServicesOwnerNotRunning(OPERATOR, "after starting the testnet bot"),
    ).resolves.toBeUndefined();
    expect(vi.mocked(console.warn).mock.calls[0][0]).toContain("owner check failed");
  });
});
