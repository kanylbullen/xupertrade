import { describe, expect, it } from "vitest";

import { formatSignedPct, formatSignedUsd, formatUsd } from "../money";

describe("formatSignedUsd", () => {
  it.each([
    [12.5, "+$12.50"],
    [-12.5, "-$12.50"],
    [-0.01, "-$0.01"],
    [1234567.891, "+$1,234,567.89"],
    [-1234.5, "-$1,234.50"],
  ])("%s → %s", (value, expected) => {
    expect(formatSignedUsd(value)).toBe(expected);
  });

  it("puts the minus before the dollar sign, never after it", () => {
    // The inline pattern this replaced rendered "$-12.50".
    expect(formatSignedUsd(-12.5)).not.toContain("$-");
  });

  it("rounds a loss as the mirror of the same gain", () => {
    expect(formatSignedUsd(0.005)).toBe("+$0.01");
    expect(formatSignedUsd(-0.005)).toBe("-$0.01");
  });

  it("renders anything that rounds to zero as unsigned $0.00", () => {
    expect(formatSignedUsd(0)).toBe("$0.00");
    expect(formatSignedUsd(-0.004)).toBe("$0.00");
    expect(formatSignedUsd(0.004)).toBe("$0.00");
  });

  it("does not print NaN", () => {
    expect(formatSignedUsd(Number.NaN)).toBe("—");
    expect(formatSignedUsd(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("formatUsd", () => {
  it("signs only negatives", () => {
    expect(formatUsd(10_000)).toBe("$10,000.00");
    expect(formatUsd(-3.2)).toBe("-$3.20");
    expect(formatUsd(-0.001)).toBe("$0.00");
  });
});

describe("formatSignedPct", () => {
  it.each([
    [1.234, "+1.23%"],
    [-1.236, "-1.24%"],
    [0.001, "0.00%"],
    [-0.004, "0.00%"],
  ])("%s → %s", (value, expected) => {
    expect(formatSignedPct(value)).toBe(expected);
  });
});
