/**
 * Global Redis stub for unit tests.
 *
 * `lib/redis.ts:getRedisClient()` lazily opens a REAL ioredis
 * connection to `REDIS_URL` (default `redis://localhost:6379/0`).
 * Several helpers take their client as a defaulted parameter —
 * `isSessionRevoked(cookie, client = getRedisClient())`,
 * `loadBotApiKey(botId, client = getRedisClient())` — so any unit test
 * that reaches such a helper without mocking it opens a live socket.
 *
 * With no Redis listening, that is not a clean failure: ioredis retries
 * with backoff, so the test hangs until vitest's 5s timeout, and
 * fail-closed helpers meanwhile return the "denied" answer. That
 * combination produced 7 opaque failures across two files (fixed in
 * PR #146) and it had already bitten twice before.
 *
 * Registering the stub here fixes the class rather than each instance:
 * no unit test can accidentally open a real connection. Test files that
 * need specific return values still mock the calling module directly —
 * a file-level `vi.mock` takes precedence over this one.
 *
 * Integration tests use a separate config (`vitest.integration.config.ts`)
 * and are unaffected.
 */
import { vi } from "vitest";

vi.mock("@/lib/redis", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/redis")>();
  const { default: RedisMock } = await import("ioredis-mock");
  // One shared instance, mirroring the real module's process-shared
  // client, so writes in a test are visible to later reads in it.
  const client = new RedisMock();
  return {
    ...actual,
    getRedisClient: () => client,
    createRedisSubscriber: () => new RedisMock(),
  };
});
