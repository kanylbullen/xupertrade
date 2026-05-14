/**
 * Host resource snapshot for /admin/server. Reads /proc directly
 * (Docker container sees the host's /proc on standard Linux), uses
 * node:fs.statfs for disk, and dockerode for container counts.
 *
 * All parsers defensive — return zeros on unexpected input rather
 * than throwing, so the page degrades to "0%" instead of a crashy
 * 500.
 */

import { readFile, statfs } from "node:fs/promises";

export type CpuStats = {
  loadAvg: [number, number, number];
  cores: number;
  usagePct: number;
};

export type MemoryStats = {
  totalMB: number;
  usedMB: number;
  freeMB: number;
  cachedMB: number;
};

export type DiskStats = {
  mount: string;
  totalGB: number;
  usedGB: number;
  freeGB: number;
  usePct: number;
};

export type DockerStats = {
  running: number;
  total: number;
};

export type ServerStats = {
  cpu: CpuStats;
  memory: MemoryStats;
  disk: DiskStats[];
  docker: DockerStats;
};

type CpuSample = { idle: number; total: number };

/** Parse /proc/stat's first "cpu" aggregate line into idle/total
 * jiffies. Returns null on malformed input. */
export function parseCpuStat(text: string): CpuSample | null {
  const line = text.split("\n").find((l) => l.startsWith("cpu "));
  if (!line) return null;
  const parts = line.trim().split(/\s+/).slice(1).map(Number);
  if (parts.length < 5 || parts.some((n) => !Number.isFinite(n))) return null;
  // user nice system idle iowait irq softirq steal guest guest_nice
  const idle = parts[3] + (parts[4] ?? 0); // include iowait per `top`
  const total = parts.reduce((a, b) => a + b, 0);
  return { idle, total };
}

/** Parse /proc/meminfo into MB. */
export function parseMemInfo(text: string): MemoryStats {
  const m = new Map<string, number>();
  for (const line of text.split("\n")) {
    const mm = line.match(/^([A-Za-z()_]+):\s+(\d+)\s+kB/);
    if (mm) m.set(mm[1], Number(mm[2]));
  }
  const totalKb = m.get("MemTotal") ?? 0;
  const freeKb = m.get("MemFree") ?? 0;
  const cachedKb = (m.get("Cached") ?? 0) + (m.get("Buffers") ?? 0);
  const availableKb = m.get("MemAvailable") ?? freeKb + cachedKb;
  return {
    totalMB: Math.round(totalKb / 1024),
    freeMB: Math.round(availableKb / 1024),
    usedMB: Math.round((totalKb - availableKb) / 1024),
    cachedMB: Math.round(cachedKb / 1024),
  };
}

/** /proc/loadavg → 3 floats. */
export function parseLoadAvg(text: string): [number, number, number] {
  const parts = text.trim().split(/\s+/).slice(0, 3).map(Number);
  if (parts.length < 3 || parts.some((n) => !Number.isFinite(n))) {
    return [0, 0, 0];
  }
  return [parts[0], parts[1], parts[2]];
}

/** /proc/cpuinfo → physical core count. Returns 1 on parse failure to
 * avoid divide-by-zero downstream. */
export function parseCpuCores(text: string): number {
  const n = text.split("\n").filter((l) => l.startsWith("processor")).length;
  return n > 0 ? n : 1;
}

/** Diff two /proc/stat samples → 0..100 % usage. Negative diffs (clock
 * adjust, counter wrap) return 0. */
export function cpuUsagePctFromSamples(a: CpuSample, b: CpuSample): number {
  const totalDiff = b.total - a.total;
  const idleDiff = b.idle - a.idle;
  if (totalDiff <= 0) return 0;
  const used = totalDiff - idleDiff;
  if (used <= 0) return 0;
  return Math.min(100, Math.max(0, (used / totalDiff) * 100));
}

async function readCpuSample(): Promise<CpuSample | null> {
  try {
    const t = await readFile("/proc/stat", "utf8");
    return parseCpuStat(t);
  } catch {
    return null;
  }
}

async function getCpu(): Promise<CpuStats> {
  let loadAvg: [number, number, number] = [0, 0, 0];
  let cores = 1;
  try {
    loadAvg = parseLoadAvg(await readFile("/proc/loadavg", "utf8"));
  } catch {}
  try {
    cores = parseCpuCores(await readFile("/proc/cpuinfo", "utf8"));
  } catch {}
  // Two samples 100ms apart → realistic instantaneous % usage.
  const a = await readCpuSample();
  await new Promise((r) => setTimeout(r, 100));
  const b = await readCpuSample();
  let usagePct = 0;
  if (a && b) usagePct = cpuUsagePctFromSamples(a, b);
  return { loadAvg, cores, usagePct };
}

async function getMemory(): Promise<MemoryStats> {
  try {
    return parseMemInfo(await readFile("/proc/meminfo", "utf8"));
  } catch {
    return { totalMB: 0, usedMB: 0, freeMB: 0, cachedMB: 0 };
  }
}

async function getDisk(): Promise<DiskStats[]> {
  const out: DiskStats[] = [];
  try {
    const s = await statfs("/");
    const totalBytes = s.blocks * s.bsize;
    const freeBytes = s.bavail * s.bsize;
    const usedBytes = totalBytes - freeBytes;
    out.push({
      mount: "/",
      totalGB: round1(totalBytes / 1e9),
      usedGB: round1(usedBytes / 1e9),
      freeGB: round1(freeBytes / 1e9),
      usePct: totalBytes > 0 ? Math.round((usedBytes / totalBytes) * 100) : 0,
    });
  } catch {}
  return out;
}

async function getDocker(): Promise<DockerStats> {
  // Lazy import — dockerode bootstraps a unix socket connection at
  // construction; doing this at module top level breaks tests that
  // mock the dashboard container without a docker socket.
  try {
    const { default: Docker } = await import("dockerode");
    const docker = new Docker();
    const containers = await docker.listContainers({ all: true });
    const running = containers.filter((c) => c.State === "running").length;
    return { running, total: containers.length };
  } catch {
    return { running: 0, total: 0 };
  }
}

function round1(n: number): number {
  return Math.round(n * 10) / 10;
}

export async function getServerStats(): Promise<ServerStats> {
  const [cpu, memory, disk, docker] = await Promise.all([
    getCpu(),
    getMemory(),
    getDisk(),
    getDocker(),
  ]);
  return { cpu, memory, disk, docker };
}
