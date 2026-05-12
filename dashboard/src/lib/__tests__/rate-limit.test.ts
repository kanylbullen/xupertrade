/**
 * Tests for the rate-limit helper (PR 3d).
 *
 * Mocks the Redis client so we can exercise the count + TTL +
 * allowed/denied branches without a live Redis. Verifies the
 * fixed-window-counter semantics: allowed up to and including
 * `max`, denied beyond, and that the EXPIRE NX setup doesn't
 * reset the window mid-flight.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { checkRateLimit } from "../rate-limit";

type PipelineExecResult = Array<[Error | null, unknown]>;

function makeRedisStub(
  pipelineResults: PipelineExecResult | null,
): {
  client: Parameters<typeof checkRateLimit>[4];
  pipeline: { incr: ReturnType<typeof vi.fn>; expire: ReturnType<typeof vi.fn>; ttl: ReturnType<typeof vi.fn>; exec: ReturnType<typeof vi.fn> };
} {
  const pipeline = {
    incr: vi.fn().mockReturnThis(),
    expire: vi.fn().mockReturnThis(),
    ttl: vi.fn().mockReturnThis(),
    exec: vi.fn().mockResolvedValue(pipelineResults),
  };
  const client = {
    multi: () => pipeline,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any;
  return { client, pipeline };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("checkRateLimit", () => {
  it("allows the first hit and reports remaining count", async () => {
    const { client, pipeline } = makeRedisStub([
      [null, 1],   // INCR returned 1
      [null, 1],   // EXPIRE returned 1 (was set)
      [null, 300], // TTL returned 300
    ]);

    const result = await checkRateLimit("test", "tenant-1", 5, 300, client);

    expect(result).toEqual({
      allowed: true,
      remaining: 4,
      resetInSeconds: 300,
    });
    expect(pipeline.incr).toHaveBeenCalledWith("ratelimit:test:tenant-1");
    expect(pipeline.expire).toHaveBeenCalledWith(
      "ratelimit:test:tenant-1",
      300,
      "NX",
    );
  });

  it("allows up to and including max", async () => {
    const { client } = makeRedisStub([
      [null, 5],
      [null, 0],
      [null, 100],
    ]);
    const r = await checkRateLimit("test", "tenant-1", 5, 300, client);
    expect(r.allowed).toBe(true);
    expect(r.remaining).toBe(0);
  });

  it("denies when count exceeds max", async () => {
    const { client } = makeRedisStub([
      [null, 6],   // 6th hit
      [null, 0],
      [null, 60],
    ]);
    const r = await checkRateLimit("test", "tenant-1", 5, 300, client);
    expect(r.allowed).toBe(false);
    expect(r.remaining).toBe(0);
    expect(r.resetInSeconds).toBe(60);
  });

  it("fails open when pipeline.exec returns null", async () => {
    // Redis pipeline aborted (connection dropped mid-exec).
    // Better to let the action through than to lock everyone out
    // because Redis hiccuped — same fail-open posture as the
    // rest of the dashboard.
    const { client } = makeRedisStub(null);
    const r = await checkRateLimit("test", "tenant-1", 5, 300, client);
    expect(r.allowed).toBe(true);
  });

  it("fails open when a per-command error appears in results", async () => {
    // INCR succeeded but TTL command errored. Without per-cmd
    // validation we'd cast the Error to a number and resetInSeconds
    // would be NaN. Should fall back to windowSeconds default.
    const { client } = makeRedisStub([
      [null, 3],
      [null, 1],
      [new Error("EXECABORT"), null],
    ]);
    const r = await checkRateLimit("test", "tenant-1", 5, 300, client);
    expect(r.allowed).toBe(true);
    expect(r.resetInSeconds).toBe(300);
    expect(Number.isFinite(r.resetInSeconds)).toBe(true);
  });

  it("clamps negative TTL to windowSeconds (Retry-After must be non-negative)", async () => {
    // TTL -2 = key vanished between INCR and TTL. Returning -2
    // as Retry-After is meaningless to the client.
    const { client } = makeRedisStub([
      [null, 6], // denied
      [null, 0],
      [null, -2],
    ]);
    const r = await checkRateLimit("test", "tenant-1", 5, 300, client);
    expect(r.allowed).toBe(false);
    expect(r.resetInSeconds).toBe(300);
  });

  it("clamps TTL of 0 to windowSeconds", async () => {
    const { client } = makeRedisStub([
      [null, 6],
      [null, 0],
      [null, 0],
    ]);
    const r = await checkRateLimit("test", "tenant-1", 5, 300, client);
    expect(r.resetInSeconds).toBe(300);
  });

  it("namespaces scope + bucket distinctly", async () => {
    const { client, pipeline } = makeRedisStub([
      [null, 1],
      [null, 1],
      [null, 300],
    ]);
    await checkRateLimit("unlock-link", "tenant-A", 5, 300, client);
    expect(pipeline.incr).toHaveBeenCalledWith(
      "ratelimit:unlock-link:tenant-A",
    );
  });
});
