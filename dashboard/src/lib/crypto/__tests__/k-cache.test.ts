/**
 * Unit tests for the session-scoped K-cache (multi-tenancy Phase 2c).
 * Uses ioredis-mock so no live Redis is required.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

// `ioredis-mock` ships ESM with no published types. It's a test-only
// dependency; safe to skip the lint here.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
import RedisMock from "ioredis-mock";
import type { Redis } from "ioredis";

import { KEY_BYTES } from "../secrets";
import { cacheKey, clearKey, loadKey } from "../k-cache";

const TENANT = "11111111-2222-3333-4444-555555555555";
const SESSION = "abc123";

let client: Redis;

beforeEach(() => {
  // Each test gets a fresh in-memory mock; cleared on dispose.
  client = new RedisMock() as unknown as Redis;
});

afterEach(async () => {
  await client.flushall();
  await client.quit();
});

function randKey(): Buffer {
  const b = Buffer.alloc(KEY_BYTES);
  for (let i = 0; i < KEY_BYTES; i++) b[i] = (i * 7 + 1) & 0xff;
  return b;
}

describe("k-cache", () => {
  it("cache + load roundtrip", async () => {
    const k = randKey();
    await cacheKey(TENANT, SESSION, k, 60, client);
    const back = await loadKey(TENANT, SESSION, client);
    expect(back).not.toBeNull();
    expect(back!.equals(k)).toBe(true);
  });

  it("loadKey returns null when nothing cached", async () => {
    expect(await loadKey(TENANT, SESSION, client)).toBeNull();
  });

  it("clearKey removes a cached entry", async () => {
    await cacheKey(TENANT, SESSION, randKey(), 60, client);
    expect(await loadKey(TENANT, SESSION, client)).not.toBeNull();
    await clearKey(TENANT, SESSION, client);
    expect(await loadKey(TENANT, SESSION, client)).toBeNull();
  });

  it("different tenants don't see each other's K", async () => {
    const otherTenant = "99999999-9999-9999-9999-999999999999";
    await cacheKey(TENANT, SESSION, randKey(), 60, client);
    expect(await loadKey(otherTenant, SESSION, client)).toBeNull();
  });

  it("different sessions for the same tenant are isolated", async () => {
    await cacheKey(TENANT, SESSION, randKey(), 60, client);
    expect(await loadKey(TENANT, "different-session", client)).toBeNull();
  });

  it("rejects wrong-length K on cache", async () => {
    await expect(
      cacheKey(TENANT, SESSION, Buffer.alloc(31), 60, client),
    ).rejects.toThrow(/K must be 32 bytes/);
  });

  it("malformed Redis value (wrong byte length) returns null + warning", async () => {
    // Manually plant a too-short base64 value to simulate corruption
    const key = `dashboard:k-cache:${TENANT}:${SESSION}`;
    await client.set(key, Buffer.alloc(31).toString("base64"));
    expect(await loadKey(TENANT, SESSION, client)).toBeNull();
  });

  it("clearKey on missing key is idempotent", async () => {
    // No throw, no side effect
    await clearKey(TENANT, SESSION, client);
    expect(await loadKey(TENANT, SESSION, client)).toBeNull();
  });
});
