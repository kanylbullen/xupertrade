/**
 * Tests for lib/caddy-admin.ts (PR 4a).
 *
 * Pure function tests for the config builders + a fetch-mock
 * test for applyCaddyConfig + pushTlsConfig branching.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  applyCaddyConfig,
  buildHttpsConfig,
  buildInternalHttpsConfig,
  pushTlsConfig,
} from "../caddy-admin";

const ORIG_ENV = { ...process.env };

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.unstubAllGlobals();
});

describe("buildHttpsConfig", () => {
  it("includes domain in subjects + Let's Encrypt + Cloudflare DNS-01 challenge", () => {
    const cfg = buildHttpsConfig({
      domain: "x.example.com",
      email: "ops@example.com",
      cfToken: "cf-tok",
    }) as Record<string, unknown>;
    const json = JSON.stringify(cfg);
    expect(json).toContain('"subjects":["x.example.com"]');
    expect(json).toContain('"module":"acme"');
    expect(json).toContain('"name":"cloudflare"');
    expect(json).toContain('"api_token":"cf-tok"');
    // 308 redirect on :80 → https://<domain>
    expect(json).toContain('"status_code":308');
    expect(json).toContain("https://x.example.com{http.request.uri}");
  });
});

describe("buildInternalHttpsConfig", () => {
  it("uses passed domain when provided", () => {
    const cfg = buildInternalHttpsConfig("internal.example.com");
    expect(JSON.stringify(cfg)).toContain('"subjects":["internal.example.com"]');
  });

  it("falls back to CADDY_HOST env then 'localhost'", () => {
    process.env.CADDY_HOST = "envhost.example.com";
    const cfg = buildInternalHttpsConfig(null);
    expect(JSON.stringify(cfg)).toContain('"subjects":["envhost.example.com"]');

    delete process.env.CADDY_HOST;
    const cfg2 = buildInternalHttpsConfig(null);
    expect(JSON.stringify(cfg2)).toContain('"subjects":["localhost"]');
  });

  it("uses 'internal' issuer (no ACME)", () => {
    const cfg = buildInternalHttpsConfig("x.example.com");
    expect(JSON.stringify(cfg)).toContain('"module":"internal"');
    expect(JSON.stringify(cfg)).not.toContain('"module":"acme"');
  });
});

describe("applyCaddyConfig", () => {
  it("returns ok on 200", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 200 })),
    );
    const r = await applyCaddyConfig({ admin: { listen: "x" } });
    expect(r.ok).toBe(true);
  });

  it("returns error message on non-200", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("malformed config", { status: 400 }),
      ),
    );
    const r = await applyCaddyConfig({});
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.message).toContain("HTTP 400");
      expect(r.message).toContain("malformed");
    }
  });

  it("returns error on network exception", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("ECONNREFUSED")),
    );
    const r = await applyCaddyConfig({});
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.message).toContain("ECONNREFUSED");
  });
});

describe("pushTlsConfig", () => {
  it("returns missing-fields without applying when enabled but incomplete", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const r = await pushTlsConfig({
      enabled: true,
      domain: "",
      email: "ops@example.com",
      cf_token: "tok",
    });
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.message).toMatch(/missing fields.*domain/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("applies internal config when disabled", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response("", { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const r = await pushTlsConfig({
      enabled: false,
      domain: "x.example.com",
      email: "",
      cf_token: "",
    });
    expect(r.ok).toBe(true);
    const sent = JSON.parse(fetchSpy.mock.calls[0][1].body as string);
    expect(JSON.stringify(sent)).toContain('"module":"internal"');
  });

  it("applies HTTPS config when enabled + complete", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response("", { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const r = await pushTlsConfig({
      enabled: true,
      domain: "x.example.com",
      email: "ops@example.com",
      cf_token: "tok",
    });
    expect(r.ok).toBe(true);
    const sent = JSON.parse(fetchSpy.mock.calls[0][1].body as string);
    expect(JSON.stringify(sent)).toContain('"module":"acme"');
  });
});
