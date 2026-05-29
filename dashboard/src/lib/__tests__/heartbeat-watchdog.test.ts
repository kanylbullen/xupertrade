/**
 * Tests for lib/heartbeat-watchdog.ts (feat/heartbeat-watchdog).
 *
 * Covers the dedup state machine and fail-safety:
 *   - bot down + no dedup key → alert sent + dedup key set
 *   - bot down + dedup key exists → NO duplicate alert
 *   - bot up + dedup key exists → recovery alert + key deleted
 *   - bot up + no dedup key → nothing
 *   - one bot's probe throwing doesn't prevent checking the others
 *   - missing API key → skip (no probe, no alert, no key mutation)
 *   - recovery send fails → dedup key is NOT deleted (retried next sweep)
 *
 * Mocks: db (Drizzle chain), bot-api (getBotApiUrl), bot-api-key
 * (loadBotApiKey), global fetch. Redis + the telegram helper are
 * injected as args to `runHeartbeatSweep`. `sendAlert` resolves to a
 * boolean (true = confirmed sent) to mirror sendOperatorTelegramAlert.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../db", () => ({
  db: { select: vi.fn() },
  tenantBots: { isRunning: {} },
}));

vi.mock("../bot-api", () => ({
  getBotApiUrl: vi.fn(),
}));

vi.mock("../bot-api-key", () => ({
  loadBotApiKey: vi.fn().mockResolvedValue("bot-key"),
}));

import { runHeartbeatSweep } from "../heartbeat-watchdog";
import { db } from "../db";
import { getBotApiUrl } from "../bot-api";
import { loadBotApiKey } from "../bot-api-key";

const mockedDbSelect = vi.mocked(db.select);
const mockedGetBotApiUrl = vi.mocked(getBotApiUrl);
const mockedLoadBotApiKey = vi.mocked(loadBotApiKey);
const fetchSpy = vi.spyOn(globalThis, "fetch");

type FakeRow = { id: string; mode: string; tenantId: string };

function fakeRow(id: string, mode = "testnet"): FakeRow {
  return { id, mode, tenantId: `tenant-${id}` };
}

/** Mock db.select().from().where() resolving to `rows`. */
function chainSelect(rows: FakeRow[]) {
  const where = vi.fn().mockResolvedValue(rows);
  const from = vi.fn().mockReturnValue({ where });
  mockedDbSelect.mockReturnValue({ from } as never);
}

/** A Redis stub backed by a Map; tracks set/del/get calls. */
function makeRedis(initial: Record<string, string> = {}) {
  const store = new Map(Object.entries(initial));
  return {
    get: vi.fn(async (k: string) => (store.has(k) ? store.get(k)! : null)),
    set: vi.fn(async (k: string, v: string) => {
      store.set(k, v);
      return "OK";
    }),
    del: vi.fn(async (k: string) => {
      const had = store.has(k);
      store.delete(k);
      return had ? 1 : 0;
    }),
    store,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
}

function heartbeatResponse(body: {
  stale?: boolean;
  age_seconds?: number | null;
}) {
  return new Response(JSON.stringify({ heartbeat: 1, ...body }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  mockedDbSelect.mockReset();
  mockedGetBotApiUrl.mockReset();
  mockedGetBotApiUrl.mockReturnValue("http://bot:8001");
  mockedLoadBotApiKey.mockReset();
  mockedLoadBotApiKey.mockResolvedValue("bot-key");
  fetchSpy.mockReset();
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "log").mockImplementation(() => {});
});

afterEach(() => {
  // Restore only the console spies; keep fetchSpy attached so the next
  // test's mockReset()/mockResolvedValue still drives globalThis.fetch.
  vi.mocked(console.warn).mockRestore();
  vi.mocked(console.log).mockRestore();
});

afterAll(() => {
  fetchSpy.mockRestore();
});

describe("runHeartbeatSweep", () => {
  it("alerts and sets dedup key when a bot is down with no prior alert", async () => {
    chainSelect([fakeRow("b1", "testnet")]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: true, age_seconds: 9999 }));
    const redis = makeRedis();
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).toHaveBeenCalledOnce();
    const text = sendAlert.mock.calls[0][0] as string;
    expect(text).toContain("offline");
    expect(text).toContain("testnet");
    expect(redis.set).toHaveBeenCalledOnce();
    const [key, value, , ttl] = redis.set.mock.calls[0];
    expect(key).toBe("dashboard:heartbeat-alert:b1");
    expect(value).toBe("alerted");
    expect(ttl).toBe(24 * 60 * 60);
  });

  it("treats a failed fetch (connection refused) as down and alerts", async () => {
    chainSelect([fakeRow("b1")]);
    fetchSpy.mockRejectedValue(new Error("ECONNREFUSED"));
    const redis = makeRedis();
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).toHaveBeenCalledOnce();
    expect(sendAlert.mock.calls[0][0]).toContain("unknown");
    expect(redis.set).toHaveBeenCalledOnce();
  });

  it("treats age_seconds > 300 as down even when stale flag is false", async () => {
    chainSelect([fakeRow("b1")]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: false, age_seconds: 400 }));
    const redis = makeRedis();
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).toHaveBeenCalledOnce();
    expect(sendAlert.mock.calls[0][0]).toContain("400s");
  });

  it("does NOT re-alert when a dedup key already exists for a down bot", async () => {
    chainSelect([fakeRow("b1")]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: true, age_seconds: 9999 }));
    const redis = makeRedis({ "dashboard:heartbeat-alert:b1": "alerted" });
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).not.toHaveBeenCalled();
    expect(redis.set).not.toHaveBeenCalled();
    expect(redis.del).not.toHaveBeenCalled();
  });

  it("sends a recovery alert and deletes the dedup key when a bot is back up", async () => {
    chainSelect([fakeRow("b1")]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: false, age_seconds: 30 }));
    const redis = makeRedis({ "dashboard:heartbeat-alert:b1": "alerted" });
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).toHaveBeenCalledOnce();
    expect(sendAlert.mock.calls[0][0]).toContain("recovered");
    expect(redis.del).toHaveBeenCalledWith("dashboard:heartbeat-alert:b1");
  });

  it("does nothing when a bot is up and there is no dedup key", async () => {
    chainSelect([fakeRow("b1")]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: false, age_seconds: 30 }));
    const redis = makeRedis();
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).not.toHaveBeenCalled();
    expect(redis.set).not.toHaveBeenCalled();
    expect(redis.del).not.toHaveBeenCalled();
  });

  it("keeps checking the remaining bots when one bot's check throws", async () => {
    chainSelect([fakeRow("b1"), fakeRow("b2", "mainnet")]);
    // b1's redis.get throws; b2 should still be processed and alerted.
    const redis = makeRedis();
    let call = 0;
    redis.get = vi.fn(async (k: string) => {
      call += 1;
      if (k === "dashboard:heartbeat-alert:b1") throw new Error("redis hiccup");
      return null;
    });
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: true, age_seconds: 9999 }));
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    // b1 threw before alerting; b2 still got its down alert.
    expect(sendAlert).toHaveBeenCalledOnce();
    const text = sendAlert.mock.calls[0][0] as string;
    expect(text).toContain("mainnet");
    expect(call).toBe(2);
  });

  it("HTML-escapes dynamic mode/tenant substrings in the alert text", async () => {
    chainSelect([{ id: "b1", mode: "test<net>", tenantId: "a&b" }]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: true, age_seconds: 9999 }));
    const redis = makeRedis();
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    const text = sendAlert.mock.calls[0][0] as string;
    expect(text).toContain("test&lt;net&gt;");
    expect(text).toContain("a&amp;b");
    expect(text).not.toContain("test<net>");
  });

  it("skips a bot with no API key — no probe, no alert, no key mutation", async () => {
    chainSelect([fakeRow("b1")]);
    mockedLoadBotApiKey.mockResolvedValue(null);
    const redis = makeRedis();
    const sendAlert = vi.fn().mockResolvedValue(true);

    await runHeartbeatSweep(redis, sendAlert);

    // Missing key is an auth-infra problem, not a confirmed outage:
    // never probe, never alert, never touch alert state.
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(sendAlert).not.toHaveBeenCalled();
    expect(redis.get).not.toHaveBeenCalled();
    expect(redis.set).not.toHaveBeenCalled();
    expect(redis.del).not.toHaveBeenCalled();
  });

  it("does NOT delete the dedup key when the recovery send fails", async () => {
    chainSelect([fakeRow("b1")]);
    fetchSpy.mockResolvedValue(heartbeatResponse({ stale: false, age_seconds: 30 }));
    const redis = makeRedis({ "dashboard:heartbeat-alert:b1": "alerted" });
    // Recovery alert send fails (returns false) — key must survive so the
    // next sweep retries the recovery notification.
    const sendAlert = vi.fn().mockResolvedValue(false);

    await runHeartbeatSweep(redis, sendAlert);

    expect(sendAlert).toHaveBeenCalledOnce();
    expect(sendAlert.mock.calls[0][0]).toContain("recovered");
    expect(redis.del).not.toHaveBeenCalled();
    expect(redis.store.get("dashboard:heartbeat-alert:b1")).toBe("alerted");
  });
});
