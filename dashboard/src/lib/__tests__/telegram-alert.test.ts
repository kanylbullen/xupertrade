/**
 * Tests for lib/telegram-alert.ts (feat/heartbeat-watchdog).
 *
 * Covers:
 *   - missing token/chat → no fetch, warns, returns false, doesn't throw
 *   - present token → POSTs correct payload to correct URL, returns true
 *   - fetch rejection → caught, returns false, no throw, warns
 *   - non-2xx Telegram response → returns false, warns, no throw
 *   - escapeTelegramHtml escapes & < >
 *
 * No real secrets — uses a fake `123:fake` token.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { escapeTelegramHtml, sendOperatorTelegramAlert } from "../telegram-alert";

const ORIG_ENV = { ...process.env };

const fetchSpy = vi.spyOn(globalThis, "fetch");

beforeEach(() => {
  delete process.env.TELEGRAM_BOT_TOKEN;
  delete process.env.TELEGRAM_CHAT_ID;
  fetchSpy.mockReset();
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  // Only restore the console spy — keep fetchSpy attached so the next
  // test's mockReset()/mockResolvedValue still drives globalThis.fetch.
  vi.mocked(console.warn).mockRestore();
});

afterAll(() => {
  fetchSpy.mockRestore();
});

describe("escapeTelegramHtml", () => {
  it("escapes &, <, > and leaves other chars intact", () => {
    expect(escapeTelegramHtml("a & b < c > d")).toBe("a &amp; b &lt; c &gt; d");
    expect(escapeTelegramHtml("plain text 123")).toBe("plain text 123");
  });
});

describe("sendOperatorTelegramAlert", () => {
  it("does not fetch, warns, and returns false when token is missing", async () => {
    process.env.TELEGRAM_CHAT_ID = "999";
    const warn = vi.spyOn(console, "warn");

    await expect(sendOperatorTelegramAlert("hello")).resolves.toBe(false);

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
  });

  it("does not fetch and returns false when chat id is missing", async () => {
    process.env.TELEGRAM_BOT_TOKEN = "123:fake";

    await expect(sendOperatorTelegramAlert("hello")).resolves.toBe(false);

    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("POSTs the correct payload and returns true on a 2xx response", async () => {
    process.env.TELEGRAM_BOT_TOKEN = "123:fake";
    process.env.TELEGRAM_CHAT_ID = "999";
    fetchSpy.mockResolvedValue(new Response("{}", { status: 200 }));

    await expect(sendOperatorTelegramAlert("🔴 bot offline")).resolves.toBe(
      true,
    );

    expect(fetchSpy).toHaveBeenCalledOnce();
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://api.telegram.org/bot123:fake/sendMessage"); // gitleaks:allow — public Telegram API host + fake token
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      chat_id: "999",
      text: "🔴 bot offline",
      parse_mode: "HTML",
    });
  });

  it("catches a fetch rejection, returns false, and does not throw", async () => {
    process.env.TELEGRAM_BOT_TOKEN = "123:fake";
    process.env.TELEGRAM_CHAT_ID = "999";
    const warn = vi.spyOn(console, "warn");
    fetchSpy.mockRejectedValue(new Error("network down"));

    await expect(sendOperatorTelegramAlert("x")).resolves.toBe(false);
    expect(warn).toHaveBeenCalled();
  });

  it("warns and returns false on a non-2xx Telegram response", async () => {
    process.env.TELEGRAM_BOT_TOKEN = "123:fake";
    process.env.TELEGRAM_CHAT_ID = "999";
    const warn = vi.spyOn(console, "warn");
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ ok: false, description: "chat not found" }), {
        status: 400,
      }),
    );

    await expect(sendOperatorTelegramAlert("x")).resolves.toBe(false);
    expect(warn).toHaveBeenCalled();
  });
});
