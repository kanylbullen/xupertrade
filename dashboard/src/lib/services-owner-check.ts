/**
 * Which of a tenant's bots actually own the side services (roadmap NU-7),
 * and the warning the start and stop routes log when that is not exactly
 * one running bot in the tenant's owner mode.
 *
 * A bot reads its env once, at spawn, so after an owner change this reads
 * the label `buildSpec` put on each running container, not the configured
 * mode. A handover half done shows as no owner or two. Decision 5.12:
 * change or stop the owner only once a new owner runs.
 */

import { and, eq } from "drizzle-orm";

import { CLAIM_PLACEHOLDER } from "./bot-claim";
import { type BotMode, statusBot } from "./bot-orchestrator";
import { db, tenantBots } from "./db";
import { containerIsServicesOwner, servicesOwnerModeFor } from "./services-owner";

type TenantRef = { id: string; isOperator?: boolean | null };
type BotRow = typeof tenantBots.$inferSelect;

/**
 * The tenant's bot row in its owner mode, running or not. /hodl and
 * /vaults read that bot: it evaluates HODL and holds the vault-tracking
 * address.
 */
export async function ownerBotRow(tenant: TenantRef): Promise<BotRow | null> {
  const rows = await db
    .select()
    .from(tenantBots)
    .where(
      and(
        eq(tenantBots.tenantId, tenant.id),
        eq(tenantBots.mode, servicesOwnerModeFor(tenant)),
      ),
    )
    .limit(1);
  return rows[0] ?? null;
}

/**
 * The tenant's running bots whose container was started as the services
 * owner. Throws when Docker can't be asked.
 */
export async function runningServicesOwners(tenantId: string): Promise<BotRow[]> {
  const rows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenantId), eq(tenantBots.isRunning, true)));
  const owners: BotRow[] = [];
  for (const row of rows) {
    if (!row.containerId || row.containerId === CLAIM_PLACEHOLDER) continue;
    const info = await statusBot(row.containerId);
    if (info && containerIsServicesOwner(info.labels)) owners.push(row);
  }
  return owners;
}

/** What is wrong with the running owners, or null when exactly one bot
 *  in the expected mode owns the services. */
export function servicesOwnerProblem(
  tenantId: string,
  expected: BotMode,
  ownerModes: readonly string[],
): string | null {
  if (ownerModes.length === 1 && ownerModes[0] === expected) return null;
  const found = ownerModes.length === 0 ? "none" : ownerModes.join(" + ");
  const effect =
    ownerModes.length === 0
      ? "Telegram, HODL, the vault scanner and key reminders are off"
      : ownerModes.length > 1
        ? "their Telegram pollers collide and alerts arrive twice"
        : "the old owner still runs them";
  return (
    `tenant ${tenantId} should have one running side-services owner, the ` +
    `${expected} bot, but has ${found}: ${effect}. Restart every other ` +
    `owner first, then the ${expected} bot.`
  );
}

/**
 * Log `servicesOwnerProblem`, if any. Never throws: it runs after a start
 * or stop has already succeeded.
 */
export async function warnIfServicesOwnerNotRunning(
  tenant: TenantRef,
  context: string,
): Promise<void> {
  try {
    const owners = await runningServicesOwners(tenant.id);
    const problem = servicesOwnerProblem(
      tenant.id,
      servicesOwnerModeFor(tenant),
      owners.map((o) => o.mode),
    );
    if (problem) console.warn(`[services-owner] ${context}: ${problem}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(
      `[services-owner] ${context}: owner check failed for tenant ${tenant.id}: ${msg}`,
    );
  }
}
