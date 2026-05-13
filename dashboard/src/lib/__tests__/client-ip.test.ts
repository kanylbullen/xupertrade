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
});
