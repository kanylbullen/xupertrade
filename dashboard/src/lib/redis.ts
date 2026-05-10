import Redis from "ioredis";

const redisUrl = process.env.REDIS_URL || "redis://localhost:6379/0";

export function createRedisSubscriber() {
  return new Redis(redisUrl);
}

/**
 * Process-shared Redis client for key-value operations (separate from
 * pub/sub subscribers, which need a dedicated connection per
 * SUBSCRIBE call). Lazy-instantiated; ioredis auto-reconnects.
 */
let _client: Redis | null = null;
export function getRedisClient(): Redis {
  if (_client === null) _client = new Redis(redisUrl);
  return _client;
}

export const CHANNEL = "hypertrade:events";
