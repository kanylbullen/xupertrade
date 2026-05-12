/**
 * Unit tests for getBotApiUrl + the API_PORT_BY_MODE convention
 * (multi-tenancy Phase 6c PR δ).
 *
 * Verifies the bot-routing helper that PR ε will wire into every
 * tenant-aware data route.
 */

import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Drizzle column refs are passed through `eq()` — we mock them as
// distinct objects so the production code's `eq(tenantBots.X, val)`
// doesn't trip on undefined. Add new columns here whenever the
// tenantBotFetch query touches a new one.
vi.mock("../db", () => ({
  db: {
    select: vi.fn(),
  },
  tenantBots: { tenantId: {}, mode: {}, isRunning: {} },
}));

vi.mock("../tenant", () => ({
  requireTenant: vi.fn(),
}));

import { API_PORT_BY_MODE } from "../bot-orchestrator";
import { getBotApiUrl, tenantBotFetch } from "../bot-api";
import { db, tenantBots } from "../db";

type TenantBotsTable = typeof tenantBots;
import { requireTenant } from "../tenant";

type TenantBotRow = TenantBotsTable["$inferSelect"];

function fakeRow(overrides: Partial<TenantBotRow> = {}): TenantBotRow {
  return {
    id: "00000000-0000-0000-0000-000000000010",
    tenantId: "00000000-0000-0000-0000-000000000001",
    mode: "paper",
    containerId: "abc123",
    containerName: "hypertrade-bot-paper",
    isRunning: true,
    telegramWebhookSecret: null,
    createdAt: new Date(),
    lastStartedAt: new Date(),
    lastStoppedAt: null,
    ...overrides,
  } as TenantBotRow;
}

describe("API_PORT_BY_MODE", () => {
  it("matches the compose convention for operator's bots", () => {
    // These ports are hardcoded in docker-compose.yml under
    // services.bot-{paper,testnet,mainnet}.environment.API_PORT.
    // If they ever drift from this constant, operator's bot URLs
    // would break — keep them in lockstep.
    expect(API_PORT_BY_MODE).toEqual({
      paper: 8000,
      testnet: 8001,
      mainnet: 8002,
    });
  });
});

describe("getBotApiUrl", () => {
  it("builds host:port from container_name + mode (paper)", () => {
    expect(getBotApiUrl(fakeRow({ mode: "paper", containerName: "hypertrade-bot-paper" })))
      .toBe("http://hypertrade-bot-paper:8000");
  });

  it("builds host:port from container_name + mode (testnet)", () => {
    expect(getBotApiUrl(fakeRow({ mode: "testnet", containerName: "hypertrade-bot-testnet" })))
      .toBe("http://hypertrade-bot-testnet:8001");
  });

  it("builds host:port from container_name + mode (mainnet)", () => {
    expect(getBotApiUrl(fakeRow({ mode: "mainnet", containerName: "hypertrade-bot-mainnet" })))
      .toBe("http://hypertrade-bot-mainnet:8002");
  });

  it("works for orchestrator-spawned per-tenant bot names", () => {
    // Orchestrator-spawned bots use the pattern hypertrade-bot-<short16>-<mode>
    // (per bot-orchestrator.ts:containerName). Same routing applies.
    expect(
      getBotApiUrl(fakeRow({
        mode: "testnet",
        containerName: "hypertrade-bot-3a2f1e4caaaa1111-testnet",
      })),
    ).toBe("http://hypertrade-bot-3a2f1e4caaaa1111-testnet:8001");
  });

  it("returns null when container_name is null (bot provisioned but not started)", () => {
    expect(getBotApiUrl(fakeRow({ containerName: null }))).toBeNull();
  });

  it("returns null when container_name is empty string", () => {
    expect(getBotApiUrl(fakeRow({ containerName: "" }))).toBeNull();
  });

  it("returns null when mode is an unknown value (defensive — DB column is varchar(16))", () => {
    // Drizzle types mode as string; the DB doesn't enforce the
    // BotMode union. A future bug or rogue insert could store e.g.
    // "spot". Routing must fail closed rather than silently use an
    // arbitrary port.
    expect(getBotApiUrl(fakeRow({ mode: "spot" as never }))).toBeNull();
    expect(getBotApiUrl(fakeRow({ mode: "" as never }))).toBeNull();
  });
});

describe("tenantBotFetch", () => {
  const mockedRequireTenant = vi.mocked(requireTenant);
  const mockedDbSelect = vi.mocked(db.select);
  const fetchSpy = vi.spyOn(globalThis, "fetch");

  beforeEach(() => {
    fetchSpy.mockReset();
  });

  afterEach(() => {
    mockedRequireTenant.mockReset();
    mockedDbSelect.mockReset();
  });

  // Restore globalThis.fetch after the suite so a leaked mock can't
  // poison subsequent test files (vitest runs files in workers but
  // node modules can still share per-worker state).
  afterAll(() => {
    fetchSpy.mockRestore();
  });

  function chainSelect(rows: Array<typeof tenantBots.$inferSelect>) {
    // Drizzle: db.select().from().where().limit()
    const limit = vi.fn().mockResolvedValue(rows);
    const where = vi.fn().mockReturnValue({ limit });
    const from = vi.fn().mockReturnValue({ where });
    mockedDbSelect.mockReturnValue({ from } as never);
    return { from, where, limit };
  }

  function tenant(overrides: Record<string, unknown> = {}) {
    return {
      id: "11111111-2222-3333-4444-555555555555",
      isOperator: false,
      ...overrides,
    } as never;
  }

  it("returns 401 (the requireTenant Response) when no session", async () => {
    const unauth = new Response(JSON.stringify({ error: "not authenticated" }), {
      status: 401,
    });
    mockedRequireTenant.mockRejectedValue(unauth);

    const res = await tenantBotFetch(
      new Request("https://x/api/positions"),
      "/api/positions",
    );
    expect(res.status).toBe(401);
  });

  it("returns 404 with mode info when tenant has no bot for the mode", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([]);

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=paper"),
      "/api/positions",
    );
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body).toEqual({
      error: "no running paper bot for tenant",
      mode: "paper",
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("filters the DB query on is_running=true (stopped rows fall to 404)", async () => {
    // The actual stopped-row filtering happens at the SQL layer
    // (where Drizzle's eq() adds an is_running=true predicate).
    // Mocked DB returns whatever rows the .where filter would
    // produce — but to make this test FAIL if someone removes
    // the `eq(tenantBots.isRunning, true)` line from production
    // code, we assert that `where` was called with an `and(...)`
    // whose chunks reference tenantBots.isRunning.
    //
    // Drizzle's eq()/and() return opaque AST objects; we can't
    // introspect them directly. Instead: spy on `where` and
    // search its argument's serialized form for the
    // tenantBots.isRunning column ref. The mock in vi.mock above
    // gives that column ref a stable identity.
    mockedRequireTenant.mockResolvedValue(tenant());
    const { where } = chainSelect([]);

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=paper"),
      "/api/positions",
    );
    expect(res.status).toBe(404);
    expect(fetchSpy).not.toHaveBeenCalled();

    // Inspect the `where` call. We can't introspect Drizzle's AST
    // cleanly, but we can serialize the call args and grep for
    // the column refs the production code touched — at minimum,
    // the and() AST will reference all three column objects.
    expect(where).toHaveBeenCalledTimes(1);
    const whereArg = where.mock.calls[0][0];
    // Drizzle's and() returns an AST with the chunks somewhere
    // under its enumerable props; JSON.stringify can choke on
    // circular refs so dig via a recursive walk.
    function visit(node: unknown, seen = new Set<unknown>()): unknown[] {
      if (node === null || typeof node !== "object") return [];
      if (seen.has(node)) return [];
      seen.add(node);
      const refs: unknown[] = [node];
      for (const v of Object.values(node as Record<string, unknown>)) {
        refs.push(...visit(v, seen));
      }
      return refs;
    }
    const allRefs = visit(whereArg);
    // The mocked tenantBots.isRunning is an empty object `{}`
    // imported from `../db`. It should be reachable from the
    // and() AST if production code includes the filter.
    // The mocked tenantBots.isRunning is reachable here by
    // identity (same `vi.mock` module instance).
    expect(allRefs).toContain(tenantBots.isRunning);
  });

  it("returns 404 when tenant_bots row exists but containerName is null", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([
      {
        id: "x",
        tenantId: "y",
        mode: "paper",
        containerId: null,
        containerName: null,
        isRunning: false,
        telegramWebhookSecret: null,
        createdAt: new Date(),
        lastStartedAt: null,
        lastStoppedAt: null,
      } as never,
    ]);

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=paper"),
      "/api/positions",
    );
    expect(res.status).toBe(404);
    const body = await res.json();
    expect(body.error).toContain("not started");
  });

  function liveBotRow(mode: "paper" | "testnet" | "mainnet" = "testnet") {
    return {
      id: "x",
      tenantId: "y",
      mode,
      containerId: "abc",
      containerName: `hypertrade-bot-${mode}`,
      isRunning: true,
      telegramWebhookSecret: null,
      createdAt: new Date(),
      lastStartedAt: new Date(),
      lastStoppedAt: null,
    } as never;
  }

  it("proxies to the resolved bot URL on success", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ positions: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=testnet"),
      "/api/positions",
    );
    expect(res.status).toBe(200);
    expect(fetchSpy).toHaveBeenCalledOnce();
    const calledUrl = fetchSpy.mock.calls[0][0] as string;
    expect(calledUrl).toBe("http://hypertrade-bot-testnet:8001/api/positions");
  });

  it("passes through 4xx from the bot with the JSON error body", async () => {
    // Bot validation/auth/permission errors must reach the dashboard
    // user verbatim — squashing them to 502 hides actionable info
    // (e.g. "invalid leverage value", "strategy disabled").
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ error: "invalid leverage" }), {
        status: 400,
        headers: { "content-type": "application/json" },
      }),
    );

    const res = await tenantBotFetch(
      new Request("https://x/api/control/state?mode=testnet"),
      "/api/control/state",
    );
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body).toEqual({ error: "invalid leverage" });
  });

  it("squashes bot 5xx to 502 with the bot body in `detail`", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    fetchSpy.mockResolvedValue(
      new Response("internal error: redis down", {
        status: 500,
        headers: { "content-type": "text/plain" },
      }),
    );

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=testnet"),
      "/api/positions",
    );
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("500");
    expect(body.detail).toContain("redis down");
  });

  it("returns 502 on network failure (fetch throws)", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    fetchSpy.mockRejectedValue(new TypeError("fetch failed"));

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=testnet"),
      "/api/positions",
    );
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("unreachable");
  });

  it("maps connection errors to stable reason codes (no infra leak)", async () => {
    // Internal hostnames/IPs/ports in raw error must NOT appear
    // in the response body — only the stable code.
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    const sensitive =
      "connect ECONNREFUSED 172.18.0.5:8001 (hypertrade-bot-testnet)";
    fetchSpy.mockRejectedValue(new Error(sensitive));

    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=testnet"),
      "/api/positions",
    );
    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.reason).toBe("connection-refused");
    // Critical: the raw message (with IP + container name) must
    // not leak into the response.
    expect(JSON.stringify(body)).not.toContain("172.18.0.5");
    expect(JSON.stringify(body)).not.toContain("hypertrade-bot");
    expect(JSON.stringify(body)).not.toContain("ECONNREFUSED");
  });

  it("maps DNS errors to dns-failed", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    fetchSpy.mockRejectedValue(
      new Error("getaddrinfo ENOTFOUND hypertrade-bot-testnet"),
    );
    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=testnet"),
      "/api/positions",
    );
    const body = await res.json();
    expect(body.reason).toBe("dns-failed");
  });

  it("maps AbortError to aborted", async () => {
    mockedRequireTenant.mockResolvedValue(tenant());
    chainSelect([liveBotRow("testnet")]);
    const abortErr = new Error("The operation was aborted");
    abortErr.name = "AbortError";
    fetchSpy.mockRejectedValue(abortErr);
    const res = await tenantBotFetch(
      new Request("https://x/api/positions?mode=testnet"),
      "/api/positions",
    );
    const body = await res.json();
    expect(body.reason).toBe("aborted");
  });
});
