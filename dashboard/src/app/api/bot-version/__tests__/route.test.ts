/**
 * GET /api/bot-version — the bot cards' build line on /settings/bots.
 * Proxies the tenant bot's `/api/version` and re-validates a 2xx.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/bot-api", () => ({
  tenantBotFetch: vi.fn(),
}));

import { tenantBotFetch } from "@/lib/bot-api";

import { GET } from "../route";

const mockedBotFetch = vi.mocked(tenantBotFetch);

const SHA = "0123456789abcdef0123456789abcdef01234567";

function req(): Request {
  return new Request("https://example.com/api/bot-version?mode=testnet");
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/bot-version", () => {
  it("asks the tenant bot for /api/version with the caller's request", async () => {
    mockedBotFetch.mockResolvedValue(
      Response.json({ sha: SHA, built_at: "2026-09-24T21:58:44Z" }),
    );
    const r = req();
    const res = await GET(r);
    expect(mockedBotFetch).toHaveBeenCalledWith(r, "/api/version");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      sha: SHA,
      built_at: "2026-09-24T21:58:44Z",
    });
  });

  it("passes errors through unchanged: no session, no bot, a pre-stamp image", async () => {
    for (const status of [401, 404, 502]) {
      const upstream = Response.json({ error: "x" }, { status });
      mockedBotFetch.mockResolvedValueOnce(upstream);
      const res = await GET(req());
      expect(res).toBe(upstream);
    }
  });

  it("turns a 2xx that is not a valid stamp into unknown", async () => {
    mockedBotFetch.mockResolvedValueOnce(
      Response.json({ sha: "<script>", built_at: "soon" }),
    );
    expect(await (await GET(req())).json()).toEqual({
      sha: "unknown",
      built_at: null,
    });

    mockedBotFetch.mockResolvedValueOnce(new Response("not json"));
    expect(await (await GET(req())).json()).toEqual({
      sha: "unknown",
      built_at: null,
    });
  });
});
