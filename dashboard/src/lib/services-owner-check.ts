/**
 * "Does this tenant have a running services owner?" (roadmap NU-7).
 *
 * If the owner bot is stopped while testnet or mainnet keeps running,
 * Telegram, HODL, the vault scanner and the key reminders go quiet with
 * nothing to say so. The orchestrator's start and stop routes call
 * `warnIfServicesOwnerNotRunning` after each action, and the heartbeat
 * watchdog checks every sweep (it also alerts; see
 * `heartbeat-watchdog.ts`). Decision 5.12: change or stop the owner
 * only once a new owner is already running.
 */

import { and, eq } from "drizzle-orm";

import { db, tenantBots } from "./db";
import { servicesOwnerMissingMessage, servicesOwnerMode } from "./services-owner";

export async function servicesOwnerRunning(tenantId: string): Promise<boolean> {
  const rows = await db
    .select({ id: tenantBots.id })
    .from(tenantBots)
    .where(
      and(
        eq(tenantBots.tenantId, tenantId),
        eq(tenantBots.mode, servicesOwnerMode()),
        eq(tenantBots.isRunning, true),
      ),
    )
    .limit(1);
  return rows.length > 0;
}

/**
 * Log a warning when the tenant has no running owner. Never throws: it
 * runs after a start or stop has already succeeded, and a failed check
 * must not turn that into an error response.
 */
export async function warnIfServicesOwnerNotRunning(
  tenantId: string,
  context: string,
): Promise<void> {
  try {
    if (await servicesOwnerRunning(tenantId)) return;
    console.warn(`[services-owner] ${context}: ${servicesOwnerMissingMessage(tenantId)}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(
      `[services-owner] ${context}: owner check failed for tenant ${tenantId}: ${msg}`,
    );
  }
}
