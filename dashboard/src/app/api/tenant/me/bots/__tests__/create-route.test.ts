/**
 * Tests for POST /api/tenant/me/bots (create-and-start).
 *
 * analysis-2026-09-15 § 5, Medium: this route spawns a container via
 * `decryptAndStart`, so it is a *start* — but it never consulted the
 * operator's `max_active_bots` cap, unlike POST /[id]/start. The
 * row-count gates already in the route count bot ROWS, not running
 * ones, so they never covered it.
 *
 * Post-merge review of #168, Low: checking the cap was not enough. The
 * row only became countable (`is_running`) after Argon2id and the
 * container spawn, long after the cap's lock was released, so two
 * concurrent creates both passed a cap of 1. The row is now inserted
 * already-running inside `reserveBotStart`'s locked transaction.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));

vi.mock("@/lib/admin/limits", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/admin/limits")
  >("@/lib/admin/limits");
  return { ...actual, reserveBotStart: vi.fn() };
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
  LimitExceededError,
  reserveBotStart,
  type DbTx,
} from "@/lib/admin/limits";
import { db } from "@/lib/db";
import { requireTenant } from "@/lib/tenant";

import { decryptAndStart } from "../_decrypt-and-start";
import { POST } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedReserve = vi.mocked(reserveBotStart);
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

/** Run the route's reserve callback against a tx that inserts through
 *  the mocked `db.insert`, as the real transaction would. */
function reserveRunsCallback() {
  mockedReserve.mockImplementationOnce(async (_tenant, reserve) =>
    reserve({ insert: db.insert } as unknown as DbTx),
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/tenant/me/bots", () => {
  it("returns 409 and starts nothing when max_active_bots is reached", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(1));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    mockedReserve.mockRejectedValueOnce(
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
    // The insert only happens inside the reservation, which refused.
    expect(mockedInsert).not.toHaveBeenCalled();
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("inserts the row already counted as running, inside the reservation", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(3));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    reserveRunsCallback();
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "ok",
      bot: { id: "bot-1" },
    } as never);

    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    expect(mockedReserve).toHaveBeenCalledWith(
      expect.objectContaining({ id: TENANT_ID, maxActiveBots: 3 }),
      expect.any(Function),
    );
    // Countable from the moment the cap passes — not from when
    // decryptAndStart finally writes is_running.
    expect(insertChain.values).toHaveBeenCalledWith(
      expect.objectContaining({
        tenantId: TENANT_ID,
        mode: "paper",
        isRunning: true,
        containerId: "claiming",
      }),
    );
    expect(mockedDecryptAndStart).toHaveBeenCalledOnce();
  });

  it("deletes the reserved row when the start fails", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(3));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    reserveRunsCallback();
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "response",
      response: Response.json({ error: "locked" }, { status: 401 }),
    });

    const res = await POST(makeReq());
    expect(res.status).toBe(401);
    expect(db.delete).toHaveBeenCalledOnce();
  });

  it("deletes the reserved row when decryptAndStart THROWS, and rethrows", async () => {
    // Review of #171, item 4: the row is inserted already counted as
    // running, and only a returned error response used to clean it up.
    // A throw (Redis down in the unlock check, a DB error on the
    // secrets read) left a row holding a cap slot with no container —
    // a cap-1 tenant stayed blocked until someone deleted it by hand.
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(1));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    reserveRunsCallback();
    const boom = new Error("ECONNREFUSED redis");
    mockedDecryptAndStart.mockRejectedValueOnce(boom);

    await expect(POST(makeReq())).rejects.toBe(boom);
    expect(db.delete).toHaveBeenCalledOnce();
  });

  it("keeps the row when the start succeeds", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(1));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    reserveRunsCallback();
    mockedDecryptAndStart.mockResolvedValueOnce({
      kind: "ok",
      bot: { id: "bot-1" },
    } as never);

    const res = await POST(makeReq());
    expect(res.status).toBe(200);
    expect(db.delete).not.toHaveBeenCalled();
  });

  it("maps a unique violation wrapped by drizzle to 409", async () => {
    // drizzle-orm >= 0.44 wraps driver errors in DrizzleQueryError and
    // keeps the postgres error (with its SQLSTATE) on `.cause`.
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(null));
    selectChain.where.mockResolvedValueOnce([{ count: 0 }]);
    mockedReserve.mockRejectedValueOnce(
      Object.assign(new Error("Failed query: insert into tenant_bots"), {
        cause: Object.assign(new Error("duplicate key"), { code: "23505" }),
      }),
    );

    const res = await POST(makeReq());
    expect(res.status).toBe(409);
    expect((await res.json()).error).toContain("already exists");
    expect(mockedDecryptAndStart).not.toHaveBeenCalled();
  });

  it("rejects an invalid mode before touching the cap", async () => {
    mockedRequireTenant.mockResolvedValueOnce(makeTenant(1));

    const res = await POST(makeReq("garbage"));
    expect(res.status).toBe(400);
    expect(mockedReserve).not.toHaveBeenCalled();
  });
});
