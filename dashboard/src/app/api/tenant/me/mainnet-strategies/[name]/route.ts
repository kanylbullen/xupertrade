import { NextResponse } from "next/server";

import {
  getMainnetOperatorCap,
  KNOWN_STRATEGIES,
} from "@/lib/admin/strategy-names";
import { getRedisClient } from "@/lib/redis";
import { requireTenant, type Tenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ name: string }> };

/**
 * POST /api/tenant/me/mainnet-strategies/:name
 * Body: { enabled: boolean }
 *
 * Adds or removes :name from this tenant's per-tenant mainnet
 * allowlist (Redis set). Enabling is rejected with 409
 * `not_in_operator_cap` if the strategy isn't permitted by the
 * operator's env cap (defense-in-depth — bot also re-checks every
 * tick via the cap-side env).
 */
export async function POST(req: Request, ctx: Params) {
  let tenant: Tenant;
  try {
    tenant = await requireTenant(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const { name } = await ctx.params;
  if (!KNOWN_STRATEGIES.includes(name)) {
    return NextResponse.json({ error: "unknown_strategy" }, { status: 404 });
  }

  const body = (await req.json().catch(() => null)) as
    | { enabled?: unknown }
    | null;
  if (!body || typeof body.enabled !== "boolean") {
    return NextResponse.json(
      { error: "body must be { enabled: boolean }" },
      { status: 400 },
    );
  }

  const key = `hypertrade:mainnet:control:enabled_strategies:${tenant.id}`;
  const redis = getRedisClient();

  if (body.enabled) {
    const cap = getMainnetOperatorCap();
    if (!cap.has(name)) {
      return NextResponse.json(
        { error: "not_in_operator_cap" },
        { status: 409 },
      );
    }
    await redis.sadd(key, name);
  } else {
    await redis.srem(key, name);
  }

  return NextResponse.json({ name, enabled: body.enabled });
}
