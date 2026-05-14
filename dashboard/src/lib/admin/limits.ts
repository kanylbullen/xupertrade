/**
 * Operator-set per-tenant limits. NULL on a tenant column = unlimited
 * (preserves legacy behavior). Enforcement points: bot start, strategy
 * enable. Bot-side reads `allowed_strategies` directly from the DB at
 * startup as defense-in-depth.
 */

import { and, count, eq, sql } from "drizzle-orm";

import { db, tenantBots, tenants, type tenants as tenantsTable } from "@/lib/db";

type TenantRow = typeof tenantsTable.$inferSelect;

export type OverCapWarning =
  | { kind: "bots_over_cap"; current: number; limit: number }
  | { kind: "strategies_over_cap"; current: number; limit: number }
  | { kind: "active_strategies_outside_allowlist"; names: string[] };

export class LimitExceededError extends Error {
  constructor(
    public readonly kind:
      | "bots_over_cap"
      | "strategies_over_cap"
      | "strategy_not_allowed",
    public readonly current: number,
    public readonly limit: number,
    public readonly extra?: Record<string, unknown>,
  ) {
    super(`limit-exceeded: ${kind} ${current}/${limit}`);
  }
}

/** Throws LimitExceededError when starting another bot would exceed
 * the tenant's max_active_bots cap. NULL cap = no-op.
 *
 * Race-safe: takes a tenant-row lock (`SELECT ... FOR UPDATE`) inside
 * a transaction so two concurrent /start calls for the SAME tenant
 * serialize on the cap check. Different tenants don't contend (each
 * locks its own row).
 */
export async function assertCanStartBot(
  tenant: Pick<TenantRow, "id" | "maxActiveBots">,
): Promise<void> {
  if (tenant.maxActiveBots === null || tenant.maxActiveBots === undefined) {
    return;
  }
  const cap = tenant.maxActiveBots;
  await db.transaction(async (tx) => {
    await tx.execute(
      sql`SELECT 1 FROM ${tenants} WHERE ${tenants.id} = ${tenant.id} FOR UPDATE`,
    );
    const rows = await tx
      .select({ n: count() })
      .from(tenantBots)
      .where(
        and(eq(tenantBots.tenantId, tenant.id), eq(tenantBots.isRunning, true)),
      );
    const current = Number(rows[0]?.n ?? 0);
    if (current >= cap) {
      throw new LimitExceededError("bots_over_cap", current, cap);
    }
  });
}

/** Throws when a strategy is not in the tenant's allowlist (NULL =
 * any strategy allowed; [] = no strategies allowed). Comparison is
 * exact-match on the registered name. */
export function assertStrategyAllowed(
  tenant: Pick<TenantRow, "allowedStrategies">,
  strategyName: string,
): void {
  const list = tenant.allowedStrategies;
  if (list === null || list === undefined) return;
  if (!list.includes(strategyName)) {
    throw new LimitExceededError("strategy_not_allowed", 0, 0, {
      strategyName,
    });
  }
}

/** Throws when enabling another strategy would exceed
 * max_active_strategies. `currentActiveCount` is supplied by the caller
 * (bot-API has the live state; we don't duplicate that source of truth). */
export function assertCanEnableStrategy(
  tenant: Pick<TenantRow, "maxActiveStrategies">,
  currentActiveCount: number,
): void {
  const cap = tenant.maxActiveStrategies;
  if (cap === null || cap === undefined) return;
  if (currentActiveCount >= cap) {
    throw new LimitExceededError("strategies_over_cap", currentActiveCount, cap);
  }
}

/** Computes warnings for a proposed limits PATCH — used by the API to
 * tell the operator how many existing rows are now over-cap. Does not
 * mutate anything. The `strategies_over_cap` and
 * `active_strategies_outside_allowlist` branches require a populated
 * `currentActiveStrategies` list and are designed for a future caller
 * (proxy enforcement PR) that has the running-strategies list per bot. */
export async function computeLimitsWarnings(
  tenantId: string,
  proposed: {
    maxActiveBots: number | null;
    maxActiveStrategies: number | null;
    allowedStrategies: string[] | null;
  },
  currentActiveStrategies: string[],
): Promise<OverCapWarning[]> {
  const warnings: OverCapWarning[] = [];

  if (proposed.maxActiveBots !== null) {
    const rows = await db
      .select({ n: count() })
      .from(tenantBots)
      .where(
        and(eq(tenantBots.tenantId, tenantId), eq(tenantBots.isRunning, true)),
      );
    const current = Number(rows[0]?.n ?? 0);
    if (current > proposed.maxActiveBots) {
      warnings.push({
        kind: "bots_over_cap",
        current,
        limit: proposed.maxActiveBots,
      });
    }
  }

  if (proposed.maxActiveStrategies !== null) {
    const current = currentActiveStrategies.length;
    if (current > proposed.maxActiveStrategies) {
      warnings.push({
        kind: "strategies_over_cap",
        current,
        limit: proposed.maxActiveStrategies,
      });
    }
  }

  if (proposed.allowedStrategies !== null) {
    const allowed = new Set(proposed.allowedStrategies);
    const outside = currentActiveStrategies.filter((n) => !allowed.has(n));
    if (outside.length > 0) {
      warnings.push({
        kind: "active_strategies_outside_allowlist",
        names: outside,
      });
    }
  }

  return warnings;
}

/** Read just the limit columns for a tenant — convenience for the
 * orchestrator path which doesn't need the rest of the row. */
export async function getTenantLimits(
  tenantId: string,
): Promise<Pick<
  TenantRow,
  "maxActiveBots" | "maxActiveStrategies" | "allowedStrategies"
> | null> {
  const rows = await db
    .select({
      maxActiveBots: tenants.maxActiveBots,
      maxActiveStrategies: tenants.maxActiveStrategies,
      allowedStrategies: tenants.allowedStrategies,
    })
    .from(tenants)
    .where(eq(tenants.id, tenantId))
    .limit(1);
  return rows[0] ?? null;
}
