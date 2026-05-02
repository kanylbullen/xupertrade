import Redis from "ioredis";

const redisUrl = process.env.REDIS_URL || "redis://localhost:6379/0";

export function createRedisSubscriber() {
  return new Redis(redisUrl);
}

export const CHANNEL = "hypertrade:events";
