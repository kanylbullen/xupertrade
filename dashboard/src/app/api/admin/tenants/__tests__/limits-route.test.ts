/**
 * Tests for PATCH /api/admin/tenants/[id]/limits.
 *
 * Focused on input validation, the not-found path (Copilot review:
 * UPDATE on missing id used to return 200), and the
 * bots_over_cap warning short-circuit.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/operator", () => ({
  requireOperator: vi.fn().mockResolvedValue({ id: "op" }),
}));

const updateChain = {
  set: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  returning: vi.fn(),
};
const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn(),
};
vi.mock("@/lib/db", () => ({
  db: {
    update: vi.fn(() => updateChain),
    select: vi.fn(() => selectChain),
  },
  tenants: { id: "id" },
  tenantBots: { tenantId: "tenantId", isRunning: "isRunning" },
}));

import { PATCH } from "../[id]/limits/route";

const TENANT_ID = "11111111-2222-3333-4444-555566667777";

function makeReq(body: unknown): Request {
  return new Request(`https://example.com/api/admin/tenants/${TENANT_ID}/limits`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function makeCtx() {
  return { params: Promise.resolve({ id: TENANT_ID }) };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("PATCH /api/admin/tenants/[id]/limits", () => {
  it("returns 404 when the tenant id does not exist", async () => {
    // UPDATE affects 0 rows → returning() resolves to []
    updateChain.returning.mockResolvedValueOnce([]);

    const res = await PATCH(
      makeReq({
        maxActiveBots: 3,
        maxActiveStrategies: 5,
        allowedStrategies: null,
      }),
      makeCtx(),
    );
    expect(res.status).toBe(404);
    const body = (await res.json()) as { error: string };
    expect(body.error).toBe("tenant_not_found");
  });

  it("returns 200 with bots_over_cap warning when running > new cap", async () => {
    updateChain.returning.mockResolvedValueOnce([{ id: TENANT_ID }]);
    selectChain.where.mockResolvedValueOnce([{ n: 5 }]);

    const res = await PATCH(
      makeReq({
        maxActiveBots: 2,
        maxActiveStrategies: null,
        allowedStrategies: null,
      }),
      makeCtx(),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      warnings: Array<{ kind: string; current: number; limit: number }>;
    };
    expect(body.warnings).toContainEqual({
      kind: "bots_over_cap",
      current: 5,
      limit: 2,
    });
  });

  it("returns 200 with no warnings when below cap", async () => {
    updateChain.returning.mockResolvedValueOnce([{ id: TENANT_ID }]);
    selectChain.where.mockResolvedValueOnce([{ n: 1 }]);

    const res = await PATCH(
      makeReq({
        maxActiveBots: 5,
        maxActiveStrategies: null,
        allowedStrategies: null,
      }),
      makeCtx(),
    );
    expect(res.status).toBe(200);
    const body = (await res.json()) as { warnings: unknown[] };
    expect(body.warnings).toEqual([]);
  });

  it("rejects invalid maxActiveBots with 400", async () => {
    const res = await PATCH(
      makeReq({
        maxActiveBots: 99,
        maxActiveStrategies: null,
        allowedStrategies: null,
      }),
      makeCtx(),
    );
    expect(res.status).toBe(400);
  });
});
