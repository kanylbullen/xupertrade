/**
 * Tests for the audit-log helper (PR 3d).
 *
 * Mocks db.insert so we can verify the call shape without a real
 * Postgres. Best-effort semantics: a DB failure swallows the
 * error and warns to console (caller's action shouldn't fail).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { insertMock, valuesMock } = vi.hoisted(() => ({
  valuesMock: vi.fn(),
  insertMock: vi.fn(),
}));

vi.mock("../db", () => ({
  db: { insert: insertMock },
  tenantAuditLog: { tenantId: "tenantId" },
}));

import { appendAuditLog } from "../audit-log";

beforeEach(() => {
  valuesMock.mockResolvedValue(undefined);
  insertMock.mockReturnValue({ values: valuesMock });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("appendAuditLog", () => {
  it("inserts a row with the right shape", async () => {
    await appendAuditLog("tenant-1", "tenant", "telegram.unlock-link.sent", {
      bot_mode: "paper",
    });

    expect(insertMock).toHaveBeenCalledOnce();
    expect(valuesMock).toHaveBeenCalledWith({
      tenantId: "tenant-1",
      actor: "tenant",
      action: "telegram.unlock-link.sent",
      contextJson: JSON.stringify({ bot_mode: "paper" }),
    });
  });

  it("defaults context to empty object when omitted", async () => {
    await appendAuditLog("tenant-1", "operator", "tenant.disabled");
    expect(valuesMock).toHaveBeenCalledWith(
      expect.objectContaining({ contextJson: "{}" }),
    );
  });

  it("swallows DB errors (best-effort write)", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    valuesMock.mockRejectedValueOnce(new Error("DB down"));

    // Must not throw — the original action already happened.
    await expect(
      appendAuditLog("tenant-1", "tenant", "passphrase.unlock"),
    ).resolves.toBeUndefined();

    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});
