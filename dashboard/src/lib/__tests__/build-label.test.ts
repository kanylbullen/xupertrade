import { describe, expect, it } from "vitest";

import { formatBuild } from "../build-label";

const SHA = "0123456789abcdef0123456789abcdef01234567";

describe("formatBuild", () => {
  it("shows the short SHA and the build time to the minute", () => {
    expect(formatBuild({ sha: SHA, built_at: "2026-09-24T21:58:44Z" })).toBe(
      "0123456, built 2026-09-24 21:58 UTC",
    );
  });

  it("shows only the short SHA when the build time is missing or malformed", () => {
    expect(formatBuild({ sha: SHA, built_at: null })).toBe("0123456");
    expect(formatBuild({ sha: SHA, built_at: "yesterday" })).toBe("0123456");
  });

  it("says unknown for the unknown sentinel, a missing answer, or non-hex text", () => {
    expect(formatBuild({ sha: "unknown", built_at: null })).toBe("unknown");
    expect(formatBuild(null)).toBe("unknown");
    expect(formatBuild(undefined)).toBe("unknown");
    expect(formatBuild({ sha: "<img src=x>", built_at: null })).toBe("unknown");
    expect(
      formatBuild({ sha: 42, built_at: null } as unknown as {
        sha: string;
        built_at: null;
      }),
    ).toBe("unknown");
  });
});
