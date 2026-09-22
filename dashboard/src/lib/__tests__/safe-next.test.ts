/**
 * Tests for `safeNext` (analysis-2026-09-15 § 5, Medium).
 *
 * The bug: the prefix checks blocked `//evil.com` but not
 * `/\evil.com`, and the WHATWG URL parser resolves the latter to
 * `https://evil.com/` — an open redirect off a successful sign-in.
 * These cases pin the origin check so the next spelling of the same
 * trick fails too.
 */

import { describe, expect, it } from "vitest";

import { safeNext } from "../safe-next";

describe("safeNext", () => {
  it("blocks the backslash open redirect", () => {
    // `new URL("/\\evil.com", base)` → https://evil.com/
    expect(safeNext("/\\evil.com")).toBe("/");
  });

  it("blocks a doubled backslash", () => {
    expect(safeNext("/\\\\evil.com")).toBe("/");
  });

  it("blocks protocol-relative targets", () => {
    expect(safeNext("//evil.com")).toBe("/");
  });

  it("allows a percent-encoded backslash (stays on our origin)", () => {
    // %5C is NOT decoded into a path separator by the parser, so this
    // resolves to <our-origin>/%5Cevil.com — a real, harmless 404.
    expect(safeNext("/%5Cevil.com")).toBe("/%5Cevil.com");
  });

  it("allows an ordinary app path with a query string", () => {
    expect(safeNext("/ok?x=1")).toBe("/ok?x=1");
  });

  it("allows a nested app path", () => {
    expect(safeNext("/trades?strategy=bb_short&page=2")).toBe(
      "/trades?strategy=bb_short&page=2",
    );
  });

  it("rejects absolute URLs", () => {
    expect(safeNext("https://evil.com/")).toBe("/");
    expect(safeNext("http://evil.com/")).toBe("/");
  });

  it("rejects relative paths that don't start with /", () => {
    expect(safeNext("evil.com")).toBe("/");
    expect(safeNext("../admin")).toBe("/");
  });

  it("refuses to bounce back to /login", () => {
    expect(safeNext("/login")).toBe("/");
    expect(safeNext("/login?next=%2F")).toBe("/");
  });

  it("refuses to land on an API route", () => {
    expect(safeNext("/api/auth/logout")).toBe("/");
  });

  it("refuses an API route reached via traversal", () => {
    // Resolves to /api/auth/logout once the parser normalises `..`.
    expect(safeNext("/x/../api/auth/logout")).toBe("/");
  });

  it("falls back to / on empty input", () => {
    expect(safeNext("")).toBe("/");
  });
});
