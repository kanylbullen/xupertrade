/**
 * Operator-set per-tenant limits. NULL on a tenant column = unlimited
 * (preserves legacy behavior). Enforcement points: bot start
 * (`reserveBotStart`, both start routes) and strategy enable (the
 * toggle route). The bot also receives `allowed_strategies` and
 * `max_active_strategies` as env at spawn (`bot-orchestrator.ts:
 * buildSpec`) and applies both at boot as defense-in-depth.
 */

import { and, count, eq, sql } from "drizzle-orm";

import { CLAIM_PLACEHOLDER, CLAIM_STALE_AFTER_SECONDS } from "@/lib/bot-claim";
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

/** The handle `db.transaction` passes to its callback. */
export type DbTx = Parameters<Parameters<typeof db.transaction>[0]>[0];

/** Check the tenant's max_active_bots cap and claim the start slot in
 * ONE transaction. Throws LimitExceededError at cap; otherwise returns
 * whatever `reserve` returns.
 *
 * `reserve` must make this start countable: flip the row's
 * `is_running` to true, or insert it that way. It runs in the same
 * transaction as the count, while the tenant row is held
 * `SELECT ... FOR UPDATE`, so a concurrent start for the same tenant
 * blocks on the lock until this one commits — and under READ
 * COMMITTED its count, a new statement, then sees the reservation.
 * Different tenants don't contend (each locks its own row).
 *
 * This used to be a bare check. The lock was released at commit, before
 * anything countable was written — create-and-start only set
 * `is_running` after the Argon2id unlock and the container spawn — so
 * two concurrent starts both counted the same number and both passed a
 * cap of 1. The lock serialized the checks, not the check-and-claim.
 *
 * Under the lock, before counting, claims older than
 * CLAIM_STALE_AFTER_SECONDS are reaped (flipped back to not-running):
 * their request died mid-start, and without this a capped tenant would
 * stay blocked by a slot no container occupies until someone stopped
 * the row by hand. See `lib/bot-claim.ts`.
 *
 * NULL cap: no lock, no reap and no count, but `reserve` still runs
 * inside a transaction so callers have one shape.
 */
export async function reserveBotStart<T>(
  tenant: Pick<TenantRow, "id" | "maxActiveBots">,
  reserve: (tx: DbTx) => Promise<T>,
): Promise<T> {
  const cap = tenant.maxActiveBots;
  return db.transaction(async (tx) => {
    if (cap !== null && cap !== undefined) {
      await tx.execute(
        sql`SELECT 1 FROM ${tenants} WHERE ${tenants.id} = ${tenant.id} FOR UPDATE`,
      );
      await tx
        .update(tenantBots)
        .set({ isRunning: false, containerId: null })
        .where(
          and(
            eq(tenantBots.tenantId, tenant.id),
            eq(tenantBots.containerId, CLAIM_PLACEHOLDER),
            // NULL last_started_at never matches: a claim that can't be
            // aged is never reaped (see lib/bot-claim.ts).
            sql`${tenantBots.lastStartedAt} <= now() - make_interval(secs => ${CLAIM_STALE_AFTER_SECONDS})`,
          ),
        );
      const rows = await tx
        .select({ n: count() })
        .from(tenantBots)
        .where(
          and(
            eq(tenantBots.tenantId, tenant.id),
            eq(tenantBots.isRunning, true),
          ),
        );
      const current = Number(rows[0]?.n ?? 0);
      if (current >= cap) {
        throw new LimitExceededError("bots_over_cap", current, cap);
      }
    }
    return reserve(tx);
  });
}

/** Stable, public-safe error codes per limit kind. Shared so the
 *  enforcement points can't drift apart on the wire shape. */
const ERROR_CODE_BY_KIND: Record<LimitExceededError["kind"], string> = {
  bots_over_cap: "bot-cap-exceeded",
  strategies_over_cap: "strategy-cap-exceeded",
  strategy_not_allowed: "strategy-not-allowed",
};

/** Map a LimitExceededError to the 409 the UI expects. `current` and
 *  `limit` are operator-set numbers about the caller's own tenant, so
 *  echoing them back leaks nothing and lets the UI say something
 *  useful instead of "409". */
export function limitExceededResponse(e: LimitExceededError): Response {
  return Response.json(
    {
      error: ERROR_CODE_BY_KIND[e.kind],
      kind: e.kind,
      current: e.current,
      limit: e.limit,
      ...(e.extra ?? {}),
    },
    { status: 409 },
  );
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
