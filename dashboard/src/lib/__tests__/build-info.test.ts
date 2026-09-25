/**
 * `GET /api/version` and the build-info file behind it (NU-4).
 *
 * The runner image bakes `build-info.json`; the route must report it,
 * and must degrade to "unknown" rather than throw or echo arbitrary
 * text when the file is missing or malformed. Same cases as
 * `bot/tests/test_version.py` — the two endpoints answer alike.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { parseBuildInfo, readBuildInfo, UNKNOWN_SHA } from "../build-info";

const SHA = "0123456789abcdef0123456789abcdef01234567";
const BUILT_AT = "2026-09-24T12:00:00Z";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "build-info-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  vi.restoreAllMocks();
  vi.resetModules();
});

function write(content: string): string {
  const file = path.join(dir, "build-info.json");
  writeFileSync(file, content);
  return file;
}

describe("readBuildInfo", () => {
  it("reads sha and build time", () => {
    const file = write(JSON.stringify({ sha: SHA, built_at: BUILT_AT }));
    expect(readBuildInfo(file)).toEqual({ sha: SHA, built_at: BUILT_AT });
  });

  it("accepts a short, uppercase sha and normalises it", () => {
    const file = write(JSON.stringify({ sha: "ABCDEF1", built_at: BUILT_AT }));
    expect(readBuildInfo(file).sha).toBe("abcdef1");
  });

  it("reports unknown when the file is missing", () => {
    expect(readBuildInfo(path.join(dir, "absent.json"))).toEqual({
      sha: UNKNOWN_SHA,
      built_at: null,
    });
  });

  it.each([
    "not json",
    '{"sha": "0123abc"', // a quote in GIT_SHA breaks the printf'd JSON
  ])("reports unknown for unparseable content %j", (content) => {
    expect(readBuildInfo(write(content))).toEqual({
      sha: UNKNOWN_SHA,
      built_at: null,
    });
  });
});

describe("parseBuildInfo", () => {
  it.each([
    null,
    ["a", "list"],
    { sha: "unknown", built_at: "yesterday" }, // compose default
    { sha: "", built_at: "" },
    { sha: "<script>alert(1)</script>", built_at: "<b>" },
    { sha: 123, built_at: 456 },
  ])("reports unknown for %j", (raw) => {
    expect(parseBuildInfo(raw)).toEqual({ sha: UNKNOWN_SHA, built_at: null });
  });
});

describe("GET /api/version", () => {
  it("returns the baked-in build info as JSON", async () => {
    write(JSON.stringify({ sha: SHA, built_at: BUILT_AT }));
    vi.spyOn(process, "cwd").mockReturnValue(dir);

    // Fresh module graph so the route's cached read happens now,
    // against the mocked cwd.
    vi.resetModules();
    const { GET } = await import("../../app/api/version/route");
    const res = await GET();

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ sha: SHA, built_at: BUILT_AT });
  });
});
