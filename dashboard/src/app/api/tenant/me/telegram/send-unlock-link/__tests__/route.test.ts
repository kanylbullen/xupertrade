/**
 * Tests for /api/tenant/me/telegram/send-unlock-link (PR 3c).
 *
 * Mocks tenant resolver, db, mintUnlockToken, and global fetch
 * (for the bot proxy call). Covers:
 *   - 412 when Telegram not linked
 *   - 503 when no running bot
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
  tenantBots: {
    tenantId: "tenantId",
    isRunning: "isRunning",
  },
}));

import { getBotApiUrl } from "@/lib/bot-api";
import { checkRateLimit } from "@/lib/rate-limit";
import { requireTenant } from "@/lib/tenant";
import { mintUnlockToken } from "@/lib/unlock-token";

import { POST } from "../route";

const mockedRequireTenant = vi.mocked(requireTenant);
const mockedGetBotApiUrl = vi.mocked(getBotApiUrl);
const mockedMintToken = vi.mocked(mintUnlockToken);
const mockedRateLimit = vi.mocked(checkRateLimit);

const TENANT_ID = "11111111-2222-3333-4444-555555555555";
const ORIG_ENV = { ...process.env };

function tenant() {
  return {
    id: TENANT_ID,
    email: "test@example.com",
    displayName: "Test",
    isOperator: false,
    passphraseSalt: null,
    passphraseVerifier: null,
  } as Awaited<ReturnType<typeof requireTenant>>;
}

function req(): Request {
  return new Request(
    "https://example.com/api/tenant/me/telegram/send-unlock-link",
    { method: "POST" },
  );
}

beforeEach(() => {
  mockedRequireTenant.mockResolvedValue(tenant());
  mockedMintToken.mockResolvedValue("signed-token-abc");
  mockedGetBotApiUrl.mockReturnValue("http://bot:8000");
  // Default: rate-limit allows. Tests that exercise denial override.
  mockedRateLimit.mockResolvedValue({
    allowed: true,
    remaining: 4,
    resetInSeconds: 900,
  });
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

  it("returns 503 when no running bot exists", async () => {
    selectChain.limit
      .mockResolvedValueOnce([
        { chatId: BigInt(1234567890) },
      ]) // linked
      .mockResolvedValueOnce([]); // no running bot
    const res = await POST(req());
    expect(res.status).toBe(503);
  });

  it("returns 500 when PUBLIC_URL not set", async () => {
    delete process.env.PUBLIC_URL;
    delete process.env.DASHBOARD_URL;
    selectChain.limit
      .mockResolvedValueOnce([{ chatId: BigInt(1234567890) }])
      .mockResolvedValueOnce([
        {
          id: "b",
          tenantId: TENANT_ID,
          mode: "paper",
          isRunning: true,
          containerName: "x",
        },
      ]);
    const res = await POST(req());
    expect(res.status).toBe(500);
  });

  it("returns 502 when bot proxy rejects", async () => {
    selectChain.limit
      .mockResolvedValueOnce([{ chatId: BigInt(1234567890) }])
      .mockResolvedValueOnce([
        {
          id: "b",
          tenantId: TENANT_ID,
          mode: "paper",
          isRunning: true,
          containerName: "x",
        },
      ]);
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
    selectChain.limit
      .mockResolvedValueOnce([{ chatId: BigInt(1234567890) }])
      .mockResolvedValueOnce([
        {
          id: "b",
          tenantId: TENANT_ID,
          mode: "paper",
          isRunning: true,
          containerName: "x",
        },
      ]);
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
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
