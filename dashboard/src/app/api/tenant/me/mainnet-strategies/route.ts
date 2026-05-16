import { NextResponse } from "next/server";

import {
  getMainnetOperatorCap,
  KNOWN_STRATEGIES,
  STRATEGIES,
} from "@/lib/admin/strategy-names";
import { getRedisClient } from "@/lib/redis";
import { requireTenant, type Tenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

/**
 * GET /api/tenant/me/mainnet-strategies
 *
 * Two-layer mainnet allowlist:
 *  - operator_cap    — what `HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES`
 *                      env permits (set by the operator in Phase).
 *  - tenant_enabled  — what this tenant has opted in to via this UI.
 * Effective = intersection. Either empty => no mainnet trading.
 *
 * `all_strategies` returns the canonical catalogue (name + summary)
 * so the UI can render a row per strategy with a description without
 * a second roundtrip.
 */
export async function GET(req: Request) {
  let tenant: Tenant;
  try {
    tenant = await requireTenant(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const operatorCap = [...getMainnetOperatorCap()];

  const redis = getRedisClient();
  const key = `hypertrade:mainnet:control:enabled_strategies:${tenant.id}`;
  const members = await redis.smembers(key);
  // Filter Redis members through the canonical catalogue so a stale
  // entry (e.g. a renamed strategy) doesn't surface as enabled.
  const known = new Set<string>(KNOWN_STRATEGIES);
  const tenantEnabled = members.filter((n) => known.has(n));

  return NextResponse.json({
    operator_cap: operatorCap,
    tenant_enabled: tenantEnabled,
    all_strategies: STRATEGIES,
  });
}
