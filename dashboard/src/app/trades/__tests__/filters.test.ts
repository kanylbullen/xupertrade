/**
 * Trades-page query-param parsing. These run on hand-editable URL
 * input, so the emphasis is on malformed values degrading to "no
 * constraint" rather than throwing or poisoning the SQL.
 */

import { describe, expect, it } from "vitest";

import { exclusiveEnd, parseDateParam, parsePageParam } from "../filters";

describe("parseDateParam", () => {
  it("parses a valid date as UTC midnight", () => {
    expect(parseDateParam("2026-07-01")?.toISOString()).toBe(
      "2026-07-01T00:00:00.000Z",
    );
  });

  it("returns undefined for absent or empty input", () => {
    expect(parseDateParam(undefined)).toBeUndefined();
    expect(parseDateParam("")).toBeUndefined();
  });

  it("rejects wrong shapes instead of throwing", () => {
    for (const bad of ["2026-7-1", "01/07/2026", "yesterday", "2026-07-01T12:00:00Z"]) {
      expect(parseDateParam(bad), bad).toBeUndefined();
    }
  });

  it("rejects dates that would silently roll over", () => {
    // `new Date("2026-02-31T00:00:00Z")` is NOT an Invalid Date — it
    // becomes March 3rd. Accepting it would filter on a day the
    // operator never asked for.
    expect(parseDateParam("2026-02-31")).toBeUndefined();
    expect(parseDateParam("2026-13-01")).toBeUndefined();
  });

  it("accepts a real leap day and rejects a fake one", () => {
    expect(parseDateParam("2024-02-29")?.toISOString()).toBe(
      "2024-02-29T00:00:00.000Z",
    );
    expect(parseDateParam("2026-02-29")).toBeUndefined();
  });
});

describe("exclusiveEnd", () => {
  it("advances one day so the end date is inclusive in the UI", () => {
    expect(exclusiveEnd("2026-07-01")?.toISOString()).toBe(
      "2026-07-02T00:00:00.000Z",
    );
  });

  it("makes a single-day range cover that whole day", () => {
    // from == to == 2026-07-01 must span [07-01T00:00, 07-02T00:00).
    const from = parseDateParam("2026-07-01")!;
    const to = exclusiveEnd("2026-07-01")!;
    const midday = new Date("2026-07-01T13:37:00.000Z");
    expect(from <= midday && midday < to).toBe(true);
  });

  it("propagates undefined for malformed input", () => {
    expect(exclusiveEnd("nonsense")).toBeUndefined();
    expect(exclusiveEnd(undefined)).toBeUndefined();
  });

  it("crosses a month boundary correctly", () => {
    expect(exclusiveEnd("2026-07-31")?.toISOString()).toBe(
      "2026-08-01T00:00:00.000Z",
    );
  });
});

describe("parsePageParam", () => {
  it("defaults to page 1", () => {
    expect(parsePageParam(undefined)).toBe(1);
    expect(parsePageParam("")).toBe(1);
  });

  it("parses a positive page", () => {
    expect(parsePageParam("4")).toBe(4);
  });

  it("clamps zero, negatives and junk to 1 (no negative OFFSET)", () => {
    for (const bad of ["0", "-3", "abc", "NaN", "1e9999"]) {
      expect(parsePageParam(bad), bad).toBeGreaterThanOrEqual(1);
    }
    expect(parsePageParam("0")).toBe(1);
    expect(parsePageParam("-3")).toBe(1);
    expect(parsePageParam("abc")).toBe(1);
  });
});
