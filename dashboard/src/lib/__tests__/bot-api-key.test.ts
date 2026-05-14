/**
 * Unit tests for the per-bot API key store (security audit H-1).
 *
 * Uses ioredis-mock so no live Redis is required. The dependency-
 * injection pattern (every helper accepts an optional client) lets
 * us pass a fresh in-memory mock per test.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import RedisMock from "ioredis-mock";
import type { Redis } from "ioredis";

import {
  _resetBotApiKeyCacheForTests,
  clearBotApiKey,
  generateBotApiKey,
  loadBotApiKey,
  persistBotApiKey,
} from "../bot-api-key";

const BOT_A = "11111111-1111-1111-1111-111111111111";
const BOT_B = "22222222-2222-2222-2222-222222222222";

let client: Redis;

beforeEach(() => {
  _resetBotApiKeyCacheForTests();
  client = new RedisMock() as unknown as Redis;
});

afterEach(async () => {
  await client.flushall();
  await client.quit();
});

describe("generateBotApiKey", () => {
  it("returns a URL-safe base64 string of the expected length", () => {
    const k = generateBotApiKey();
    // 32 random bytes → 43 chars in base64url (no padding).
    expect(k).toHaveLength(43);
    expect(k).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("produces a different key every call (entropy sanity)", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 100; i++) seen.add(generateBotApiKey());
    expect(seen.size).toBe(100);
  });
});

describe("persist + load roundtrip", () => {
  it("persisted key is readable by loadBotApiKey", async () => {
    const k = generateBotApiKey();
    await persistBotApiKey(BOT_A, k, client);
    const back = await loadBotApiKey(BOT_A, client);
    expect(back).toBe(k);
  });

  it("loadBotApiKey returns null for an unknown bot", async () => {
    expect(await loadBotApiKey(BOT_A, client)).toBeNull();
  });

  it("different bots have different keys (no cross-bot leakage)", async () => {
    await persistBotApiKey(BOT_A, "key-a", client);
    await persistBotApiKey(BOT_B, "key-b", client);
    expect(await loadBotApiKey(BOT_A, client)).toBe("key-a");
    expect(await loadBotApiKey(BOT_B, client)).toBe("key-b");
  });

  it("persist overwrites a previous key (restart rotation)", async () => {
    await persistBotApiKey(BOT_A, "old", client);
    await persistBotApiKey(BOT_A, "new", client);
    expect(await loadBotApiKey(BOT_A, client)).toBe("new");
  });
});

describe("clearBotApiKey", () => {
  it("removes the persisted key", async () => {
    await persistBotApiKey(BOT_A, "x", client);
    expect(await loadBotApiKey(BOT_A, client)).toBe("x");
    await clearBotApiKey(BOT_A, client);
    expect(await loadBotApiKey(BOT_A, client)).toBeNull();
  });

  it("is idempotent on a missing key", async () => {
    await expect(clearBotApiKey(BOT_A, client)).resolves.toBeUndefined();
  });

  it("only clears the targeted bot", async () => {
    await persistBotApiKey(BOT_A, "a", client);
    await persistBotApiKey(BOT_B, "b", client);
    await clearBotApiKey(BOT_A, client);
    expect(await loadBotApiKey(BOT_A, client)).toBeNull();
    expect(await loadBotApiKey(BOT_B, client)).toBe("b");
  });
});

describe("in-process cache", () => {
  it("loadBotApiKey caches the result for subsequent calls (no Redis hit)", async () => {
    await persistBotApiKey(BOT_A, "cached", client);
    expect(await loadBotApiKey(BOT_A, client)).toBe("cached");

    // Mutate Redis directly behind the cache's back; loadBotApiKey
    // should still return the cached value within the TTL window.
    await client.set("tenant:bot:" + BOT_A + ":api_key", "changed");
    expect(await loadBotApiKey(BOT_A, client)).toBe("cached");
  });

  it("clearBotApiKey invalidates the cache (so a re-read returns null)", async () => {
    await persistBotApiKey(BOT_A, "cached", client);
    await loadBotApiKey(BOT_A, client); // populate cache
    await clearBotApiKey(BOT_A, client);
    expect(await loadBotApiKey(BOT_A, client)).toBeNull();
  });

  it("persistBotApiKey refreshes the cache (so a re-read sees the new value)", async () => {
    await persistBotApiKey(BOT_A, "old", client);
    await loadBotApiKey(BOT_A, client); // populate cache
    await persistBotApiKey(BOT_A, "new", client);
    expect(await loadBotApiKey(BOT_A, client)).toBe("new");
  });
});
