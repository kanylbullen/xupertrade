/**
 * Tests for POST /api/tenant/me/bots (create-and-start).
 *
 * analysis-2026-09-15 § 5, Medium: this route spawns a container via
 * `decryptAndStart`, so it is a *start* — but it never called
 * `assertCanStartBot`, unlike POST /[id]/start. The operator's
 * `max_active_bots` cap therefore depended on which button the tenant
 * pressed. The row-count gates already in the route count bot ROWS,
 * not running ones, so they never covered it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

vi.mock("@/lib/admin/limits", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/admin/limits")
  >("@/lib/admin/limits");
  return { ...actual, assertCanStartBot: vi.fn() };
});

// db.select() chain: the route's existing-bot count ends on
// `.where()`, which resolves to the count rows.
const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn(),
};
const insertChain = {
  values: vi.fn().mockResolvedValue(undefined),
};
const deleteChain = {
  where: vi.fn().mockReturnValue({
    catch: vi.fn().mockResolvedValue(undefined),
  }),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
    insert: vi.fn(() => insertChain),
    delete: vi.fn(() => deleteChain),
  },
  tenantBots: { id: "id", tenantId: "tenantId", mode: "mode" },
  tenantSecrets: { key: "key", tenantId: "tenantId" },
}));

vi.mock("../_decrypt-and-start", () => ({
  decryptAndStart: vi.fn(),
}));

import {
  assertCanStartBot,
  LimitExceededError,
} from "@/lib/admin/limits";
import { db } from "@/lib/db";
import { requireTenant } from "@/lib/tenant";

import { decryptAndStart } from "../_decrypt-and-start";
import { POST } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedAssertCanStartBot = vi.mocked(assertCanStartBot);
const mockedDecryptAndStart = vi.mocked(decryptAndStart);
const mockedInsert = vi.mocked(db.insert);

const TENANT_ID = "3a2f1e4c-aaaa-bbbb-cccc-111122223333";

function makeReq(mode = "paper"): Request {
  return new Request("https://example.com/api/tenant/me/bots", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

function makeTenant(maxActiveBots: number | null = null) {
  return {
    id: TENANT_ID,
    multiBotEnabled: true,
    maxActiveBots,
  } as Awaited<ReturnType<typeof requireTenant>>;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/tenant/me/bots", () => {
  it("returns 409 and starts nothing when max_active_bots is reached", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(1));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    mockedAssertCanStartBot.mockRejectedValueOnce(
      new LimitExceededError("bots_over_cap", 1, 1),
    );

    const res = await POST(makeReq());
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({
      error: "bot-cap-exceeded",
      kind: "bots_over_cap",
      current: 1,
      limit: 1,
    });
    // The cap is checked BEFORE the row reservation, so no row is
    // created and then rolled back.
    expect(mockedInsert).not.toHaveBeenCalled();
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("proceeds to start when the cap allows it", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(3));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    mockedAssertCanStartBot.mockResolvedValueOnce(undefined);
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "bot",
      bot: { id: "bot-1" },
    } as never);

    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    expect(mockedAssertCanStartBot).toHaveBeenCalledWith(
      expect.objectContaining({ id: TENANT_ID, maxActiveBots: 3 }),
    );
    expect(mockedDecryptAndStart).toHaveBeenCalledOnce();
  });

  it("rejects an invalid mode before touching the cap", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(1));

    const res = await POST(makeReq("garbage"));
    expect(res.status).toBe(400);
    expect(mockedAssertCanStartBot).not.toHaveBeenCalled();
  });
});
