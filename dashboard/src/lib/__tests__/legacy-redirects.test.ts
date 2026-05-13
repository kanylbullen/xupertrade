/**
 * Unit tests for the legacy-URL 308 redirects shipped with the
 * sidebar nav cutover (PR B + C of the nav refactor):
 *
 *   - `/` (root) → `/overview/<mode ?? paper>` preserving `?mode=`
 *   - `/status` → `/settings/bots`
 *   - `/trades?mode=<x>` → `/trades?filter=<x>` (legacy bookmarks)
 *
 * `permanentRedirect` from `next/navigation` throws an opaque error in
 * server components — we mock it to capture the target URL and assert
 * on what was passed.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";

const REDIRECT_SENTINEL = "NEXT_PERMANENT_REDIRECT";

vi.mock("next/navigation", () => ({
  permanentRedirect: vi.fn((url: string) => {
    const err = new Error(REDIRECT_SENTINEL);
    (err as Error & { url: string }).url = url;
    throw err;
  }),
  redirect: vi.fn(),
  notFound: vi.fn(),
}));

import { permanentRedirect } from "next/navigation";

const redirectMock = vi.mocked(permanentRedirect);

beforeEach(() => {
  redirectMock.mockClear();
});

async function captureRedirect(fn: () => Promise<unknown> | unknown): Promise<string> {
  try {
    await fn();
  } catch (e) {
    if ((e as Error).message === REDIRECT_SENTINEL) {
      return (e as Error & { url: string }).url;
    }
    throw e;
  }
  throw new Error("expected a redirect, but the function returned normally");
}

describe("/ → /overview/<mode> redirect", () => {
  it("preserves a valid ?mode=testnet", async () => {
    const RootIndex = (await import("../../app/page")).default;
    const url = await captureRedirect(() =>
      RootIndex({ searchParams: Promise.resolve({ mode: "testnet" }) }),
    );
    expect(url).toBe("/overview/testnet");
  });

  it("defaults to paper when ?mode is missing", async () => {
    const RootIndex = (await import("../../app/page")).default;
    const url = await captureRedirect(() =>
      RootIndex({ searchParams: Promise.resolve({}) }),
    );
    expect(url).toBe("/overview/paper");
  });

  it("falls back to paper on an unknown ?mode value", async () => {
    const RootIndex = (await import("../../app/page")).default;
    const url = await captureRedirect(() =>
      RootIndex({ searchParams: Promise.resolve({ mode: "live" }) }),
    );
    expect(url).toBe("/overview/paper");
  });
});

describe("/status → /settings/bots redirect", () => {
  it("redirects unconditionally", async () => {
    const StatusPage = (await import("../../app/status/page")).default;
    const url = await captureRedirect(() => StatusPage());
    expect(url).toBe("/settings/bots");
  });
});

describe("/trades legacy ?mode= redirect", () => {
  // The trades page doesn't redirect when only ?filter= is set; only
  // ?mode= triggers the legacy-bookmark fallback.
  it("?mode=mainnet → ?filter=mainnet", async () => {
    // The trades page imports server-only code. We don't render it
    // here; the mode-redirect branch is reachable before the DB call.
    vi.doMock("../tenant-server", () => ({
      requireTenantServer: vi.fn(),
    }));
    vi.doMock("../queries", () => ({
      getRecentTrades: vi.fn(async () => []),
    }));
    const TradesPage = (await import("../../app/trades/page")).default;
    const url = await captureRedirect(() =>
      TradesPage({ searchParams: Promise.resolve({ mode: "mainnet" }) }),
    );
    expect(url).toBe("/trades?filter=mainnet");
  });
});
