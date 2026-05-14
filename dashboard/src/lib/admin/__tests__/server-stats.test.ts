import { describe, expect, it } from "vitest";

import {
  cpuUsagePctFromSamples,
  parseCpuCores,
  parseCpuStat,
  parseLoadAvg,
  parseMemInfo,
} from "../server-stats";

describe("server-stats parsers", () => {
  it("parses /proc/stat aggregate cpu line", () => {
    const text =
      "cpu  100 5 50 1000 20 0 10 0 0 0\ncpu0 50 2 25 500 10 0 5 0 0 0\n";
    const s = parseCpuStat(text);
    expect(s).not.toBeNull();
    expect(s!.idle).toBe(1020); // idle + iowait
    expect(s!.total).toBe(1185);
  });

  it("returns null for malformed cpu line", () => {
    expect(parseCpuStat("garbage\n")).toBeNull();
    expect(parseCpuStat("cpu xx yy\n")).toBeNull();
  });

  it("parses /proc/meminfo into MB", () => {
    const text =
      "MemTotal:       16000000 kB\nMemFree:        2000000 kB\nMemAvailable:  10000000 kB\nBuffers:        500000 kB\nCached:        4000000 kB\n";
    const m = parseMemInfo(text);
    expect(m.totalMB).toBe(15625);
    expect(m.usedMB).toBe(15625 - Math.round(10000000 / 1024));
    expect(m.freeMB).toBe(Math.round(10000000 / 1024));
    expect(m.cachedMB).toBe(Math.round(4500000 / 1024));
  });

  it("falls back to free+cached when MemAvailable absent", () => {
    const text = "MemTotal: 1000 kB\nMemFree: 200 kB\nCached: 100 kB\n";
    const m = parseMemInfo(text);
    expect(m.freeMB).toBe(0); // 300 / 1024 rounds to 0
  });

  it("parses /proc/loadavg", () => {
    expect(parseLoadAvg("0.5 1.2 2.3 1/300 12345")).toEqual([0.5, 1.2, 2.3]);
    expect(parseLoadAvg("garbage")).toEqual([0, 0, 0]);
  });

  it("counts processors in /proc/cpuinfo", () => {
    const text = "processor: 0\nfoo\nprocessor: 1\nprocessor: 2\n";
    expect(parseCpuCores(text)).toBe(3);
    expect(parseCpuCores("nothing")).toBe(1);
  });

  it("computes cpu usage % from two samples", () => {
    const a = { idle: 100, total: 200 };
    const b = { idle: 150, total: 300 };
    // total diff 100, idle diff 50, used 50 → 50%
    expect(cpuUsagePctFromSamples(a, b)).toBeCloseTo(50);
  });

  it("returns 0 on negative diffs", () => {
    expect(cpuUsagePctFromSamples({ idle: 200, total: 300 }, { idle: 100, total: 200 })).toBe(
      0,
    );
  });

  it("clamps to [0,100]", () => {
    const a = { idle: 100, total: 100 };
    const b = { idle: 100, total: 200 };
    expect(cpuUsagePctFromSamples(a, b)).toBe(100);
  });
});
