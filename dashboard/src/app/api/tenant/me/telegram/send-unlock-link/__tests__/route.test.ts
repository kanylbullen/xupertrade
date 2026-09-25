/**
 * Tests for /api/tenant/me/telegram/send-unlock-link (PR 3c).
 *
 * Mocks tenant resolver, db, the running-owner lookup, mintUnlockToken,
 * and global fetch (for the bot proxy call). Covers:
 *   - 412 when Telegram not linked
 *   - 503 when no running bot was started as the services owner
 *   - the sender is a running owner container, preferring the tenant's
 *     owner mode during a handover (NU-7)
 *   - 500 when PUBLIC_URL not set
 *   - 502 when bot proxy returns non-ok
 *   - happy path forwards to bot's internal endpoint
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/tenant", () => ({
  requireTenant: vi.fn(),
}));
vi.mock("@/lib/unlock-token", () => ({
  mintUnlockToken: vi.fn(),
}));
vi.mock("@/lib/bot-api", () => ({
  getBotApiUrl: vi.fn(),
}));
// H-1: per-bot API key replaces process.env.API_KEY for proxy auth.
vi.mock("@/lib/bot-api-key", () => ({
  loadBotApiKey: vi.fn().mockResolvedValue("test-key"),
}));
vi.mock("@/lib/rate-limit", () => ({
  checkRateLimit: vi.fn(),
}));
vi.mock("@/lib/audit-log", () => ({
  appendAuditLog: vi.fn().mockResolvedValue(undefined),
}));
// NU-7: which bots own Telegram is read from the running containers.
vi.mock("@/lib/services-owner-check", () => ({
  runningServicesOwners: vi.fn(),
}));

const selectChain = {
  from: vi.fn().mockReturnThis(),
  where: vi.fn().mockReturnThis(),
  limit: vi.fn(),
};
vi.mock("@/lib/db", () => ({
  db: {
    select: vi.fn(() => selectChain),
  },
  tenantTelegramLinks: {
    tenantId: "tenantId",
    telegramChatId: "telegramChatId",
  },
}));

import { getBotApiUrl } from "@/lib/bot-api";
import { loadBotApiKey } from "@/lib/bot-api-key";
import { checkRateLimit } from "@/lib/rate-limit";
import { runningServicesOwners } from "@/lib/services-owner-check";
import { requireTenant } from "@/lib/tenant";
import { mintUnlockToken } from "@/lib/unlock-token";

import { POST } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedGetBotApiUrl = vi.mocked(getBotApiUrl);
const mockedMintToken = vi.mocked(mintUnlockToken);
const mockedRateLimit = vi.mocked(checkRateLimit);
const mockedOwners = vi.mocked(runningServicesOwners);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";
const ORIG_ENV = { ...process.env };

function tenant(isOperator = false) {
  return {
    id: TENANT_ID,
    email: "test@example.com",
    displayName: "Test",
    isOperator,
    passphraseSalt: null,
    passphraseVerifier: null,
  } as Awaited<ReturnType<typeof requireTenant>>;
}

function botRow(id: string, mode: string) {
  return {
    id,
    tenantId: TENANT_ID,
    mode,
    isRunning: true,
    containerName: `x-${mode}`,
  } as Awaited<ReturnType<typeof runningServicesOwners>>[number];
}

function req(): Request {
  return new Request(
    "https://example.com/api/tenant/me/telegram/send-unlock-link",
    { method: "POST" },
  );
}

beforeEach(() => {
  delete process.env.HYPERTRADE_SERVICES_OWNER_MODE;
  mockedRequireTenant.mockResolvedValue(tenant());
  mockedMintToken.mockResolvedValue("signed-token-abc");
  mockedGetBotApiUrl.mockReturnValue("http://bot:8000");
  // Default: rate-limit allows. Tests that exercise denial override.
  mockedRateLimit.mockResolvedValue({
    allowed: true,
    remaining: 4,
    resetInSeconds: 900,
  });
  mockedOwners.mockResolvedValue([botRow("b", "mainnet")]);
  process.env.PUBLIC_URL = "https://example.com";
  process.env.API_KEY = "test-key";
});

afterEach(() => {
  vi.clearAllMocks();
  selectChain.limit.mockReset();
  process.env = { ...ORIG_ENV };
});

describe("POST /api/tenant/me/telegram/send-unlock-link", () => {
  it("returns 412 when telegram not linked", async () => {
    selectChain.limit.mockResolvedValueOnce([]); // no link row
    const res = await POST(req());
    expect(res.status).toBe(412);
    const body = await res.json();
    expect(body.error).toContain("telegram");
  });

  it.each([
    [false, "mainnet"],
    [true, "paper"],
  ])(
    "returns 503 naming the owner mode when no owner runs (operator=%s → %s)",
    async (isOperator, mode) => {
      mockedRequireTenant.mockResolvedValueOnce(tenant(isOperator));
      selectChain.limit.mockResolvedValueOnce([{ chatId: BigInt(1234567890) }]);
      mockedOwners.mockResolvedValueOnce([]);
      const res = await POST(req());
      expect(res.status).toBe(503);
      expect(mockedOwners).toHaveBeenCalledWith(TENANT_ID);
      expect((await res.json()).error).toContain(`the ${mode} bot`);
    },
  );

  it("sends through the owner in the tenant's owner mode during a handover", async () => {
    mockedRequireTenant.mockResolvedValueOnce(tenant(true));
    selectChain.limit.mockResolvedValueOnce([{ chatId: BigInt(1234567890) }]);
    mockedOwners.mockResolvedValueOnce([botRow("old", "mainnet"), botRow("new", "paper")]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 200 })));
    const res = await POST(req());
    expect(res.status).toBe(200);
    expect(vi.mocked(loadBotApiKey)).toHaveBeenCalledWith("new");
    vi.unstubAllGlobals();
  });

  it("uses the bot that still owns Telegram before the new owner restarts", async () => {
    // Operator's owner is now paper, but only the old mainnet container
    // was started with Telegram on: it is the one that can send.
    mockedRequireTenant.mockResolvedValueOnce(tenant(true));
    selectChain.limit.mockResolvedValueOnce([{ chatId: BigInt(1234567890) }]);
    mockedOwners.mockResolvedValueOnce([botRow("old", "mainnet")]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 200 })));
    const res = await POST(req());
    expect(res.status).toBe(200);
    expect(vi.mocked(loadBotApiKey)).toHaveBeenCalledWith("old");
    vi.unstubAllGlobals();
  });

  it("returns 500 when PUBLIC_URL not set", async () => {
    delete process.env.PUBLIC_URL;
    delete process.env.DASHBOARD_URL;
    selectChain.limit.mockResolvedValueOnce([{ chatId: BigInt(1234567890) }]);
    const res = await POST(req());
    expect(res.status).toBe(500);
  });

  it("returns 502 when bot proxy rejects", async () => {
    selectChain.limit.mockResolvedValueOnce([{ chatId: BigInt(1234567890) }]);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "telegram not configured" }), {
        status: 503,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const res = await POST(req());
    expect(res.status).toBe(502);
    vi.unstubAllGlobals();
  });

  it("happy path forwards to bot's internal endpoint with chat_id + signed URL", async () => {
    selectChain.limit.mockResolvedValueOnce([{ chatId: BigInt(1234567890) }]);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ sent: true }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const res = await POST(req());
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://bot:8000/api/internal/send-unlock-link");
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-Api-Key"]).toBe("test-key");
    const sentBody = JSON.parse(init?.body as string);
    expect(sentBody.chat_id).toBe("1234567890");
    expect(sentBody.url).toContain("https://example.com/unlock?token=");
    expect(sentBody.url).toContain("signed-token-abc");
    vi.unstubAllGlobals();
  });

  it("returns 429 when rate-limited before doing any work", async () => {
    mockedRateLimit.mockResolvedValueOnce({
      allowed: false,
      remaining: 0,
      resetInSeconds: 600,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const res = await POST(req());
    expect(res.status).toBe(429);
    expect(res.headers.get("Retry-After")).toBe("600");
    const body = await res.json();
    expect(body.retryAfterSeconds).toBe(600);
    // DB lookup + bot fetch must NOT have happened.
    expect(selectChain.limit).not.toHaveBeenCalled();
    expect(mockedOwners).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
