/**
 * NU-2 restore guard, dashboard half.
 *
 * A bot reads a missing sentinel as "Redis lost its control state" and
 * boots holding: no opens, no market-close of an exchange position
 * without a DB row, until a human clears the hold. A bot that did not
 * exist until now has no state to lose, so creating it writes the
 * sentinel. NX: an existing sentinel is never touched. The key must
 * match `_key()` in bot/hypertrade/engine/control.py.
 */

import "server-only";

import type { Redis } from "ioredis";

import type { BotMode } from "./bot-orchestrator";
import { getRedisClient } from "./redis";

export function restoreSentinelKey(tenantId: string, mode: BotMode): string {
  return `hypertrade:${mode}:t:${tenantId}:control:sentinel`;
}

export async function seedRestoreSentinel(
  tenantId: string,
  mode: BotMode,
  client: Redis = getRedisClient(),
): Promise<void> {
  const now = String(Math.floor(Date.now() / 1000));
  await client.set(restoreSentinelKey(tenantId, mode), now, "NX");
}
