/**
 * Tests for getClientIp (PR #92 Copilot review fix).
 *
 * The previous implementation took the LEFT-most x-forwarded-for
 * value, which is attacker-controlled when the proxy appends rather
 * than overwrites. This test pins the new RIGHT-most behavior so a
 * future regression is caught immediately.
 */

import { describe, expect, it } from "vitest";

import { getClientIp } from "../client-ip";

function makeReq(headers: Record<string, string>): Request {
  return new Request("https://example.com/", { headers });
}

describe("getClientIp", () => {
  it("prefers CF-Connecting-IP over everything else", () => {
    expect(
      getClientIp(
        makeReq({
          "cf-connecting-ip": "203.0.113.5",
          "x-forwarded-for": "1.2.3.4, 5.6.7.8",
          "x-real-ip": "9.9.9.9",
        }),
      ),
    ).toBe("203.0.113.5");
  });

  it("returns the right-most x-forwarded-for entry (proxy-trusted hop)", () => {
    // An attacker spoofing `X-Forwarded-For: 1.1.1.1` while their
    // real IP is 198.51.100.7 reaches Caddy, which APPENDS its
    // observed src → `X-Forwarded-For: 1.1.1.1, 198.51.100.7`. The
    // right-most value is the trustworthy one.
    expect(
      getClientIp(makeReq({ "x-forwarded-for": "1.1.1.1, 198.51.100.7" })),
    ).toBe("198.51.100.7");
  });

  it("handles a single-value x-forwarded-for", () => {
    expect(getClientIp(makeReq({ "x-forwarded-for": "203.0.113.7" }))).toBe(
      "203.0.113.7",
    );
  });

  it("falls back to x-real-ip when x-forwarded-for is absent", () => {
    expect(getClientIp(makeReq({ "x-real-ip": "198.51.100.10" }))).toBe(
      "198.51.100.10",
    );
  });

  it("returns 'unknown' when no header is present", () => {
    expect(getClientIp(makeReq({}))).toBe("unknown");
  });

  it("ignores empty values in x-forwarded-for", () => {
    expect(
      getClientIp(makeReq({ "x-forwarded-for": ",, 198.51.100.20" })),
    ).toBe("198.51.100.20");
  });

  // analysis-2026-09-15 § 5, Medium: CF-Connecting-IP is only
  // trustworthy because Caddy deletes it on the LAN path and
  // cloudflared is the only other thing that can set it. These pin
  // the dashboard half of that contract — the proxy half lives in
  // `caddy/Caddyfile` and `lib/caddy-admin.ts:dashboardReverseProxy`.
  it("ignores a CF-Connecting-IP that isn't an IP address", () => {
    // A value that survives the proxy but isn't an address is not a
    // client IP; letting it through would put attacker-chosen text
    // inside the Redis rate-limit key.
    expect(
      getClientIp(
        makeReq({
          "cf-connecting-ip": "bucket-of-my-choosing",
          "x-forwarded-for": "198.51.100.7",
        }),
      ),
    ).toBe("198.51.100.7");
  });

  it("accepts an IPv6 CF-Connecting-IP", () => {
    expect(
      getClientIp(makeReq({ "cf-connecting-ip": "2606:4700:4700::1111" })),
    ).toBe("2606:4700:4700::1111");
  });

  it("does not fall left past a non-parsing right-most XFF entry", () => {
    // Everything left of the right-most entry is client-supplied.
    // "our upstream wrote something odd" must not become "trust the
    // attacker's value instead".
    expect(
      getClientIp(makeReq({ "x-forwarded-for": "1.1.1.1, not-an-ip" })),
    ).toBe("unknown");
  });

  it("ignores a non-IP x-real-ip", () => {
    expect(getClientIp(makeReq({ "x-real-ip": "../../etc" }))).toBe("unknown");
  });
});
