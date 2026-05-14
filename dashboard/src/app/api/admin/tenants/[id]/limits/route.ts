import { and, count, eq } from "drizzle-orm";

import { db, tenantBots, tenants } from "@/lib/db";
import type { OverCapWarning } from "@/lib/admin/limits";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ id: string }> };

const MAX_BOTS = 10;
const MAX_STRATS = 30;

function validInt(v: unknown, max: number): v is number {
  return typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= max;
}

export async function PATCH(req: Request, ctx: Params): Promise<Response> {
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const { id } = await ctx.params;
  const body = (await req.json().catch(() => null)) as Record<string, unknown> | null;
  if (!body) return Response.json({ error: "invalid json" }, { status: 400 });

  let maxActiveBots: number | null;
  if (body.maxActiveBots === null) maxActiveBots = null;
  else if (validInt(body.maxActiveBots, MAX_BOTS)) maxActiveBots = body.maxActiveBots;
  else return Response.json({ error: `maxActiveBots must be null or 0..${MAX_BOTS}` }, { status: 400 });

  let maxActiveStrategies: number | null;
  if (body.maxActiveStrategies === null) maxActiveStrategies = null;
  else if (validInt(body.maxActiveStrategies, MAX_STRATS))
    maxActiveStrategies = body.maxActiveStrategies;
  else
    return Response.json(
      { error: `maxActiveStrategies must be null or 0..${MAX_STRATS}` },
      { status: 400 },
    );

  let allowedStrategies: string[] | null;
  if (body.allowedStrategies === null) allowedStrategies = null;
  else if (
    Array.isArray(body.allowedStrategies) &&
    body.allowedStrategies.every((s) => typeof s === "string" && s.length > 0)
  ) {
    allowedStrategies = body.allowedStrategies as string[];
  } else {
    return Response.json(
      { error: "allowedStrategies must be null or string[]" },
      { status: 400 },
    );
  }

  // Validate strategy names against the known registered set when an
  // allowlist is supplied. Pulled from the bot via a static list keeps
  // this dashboard endpoint testable without a live bot — names are
  // duplicated in @/lib/admin/strategy-names; if a new strategy is
  // registered, that list needs updating.
  if (allowedStrategies !== null && allowedStrategies.length > 0) {
    const { KNOWN_STRATEGIES } = await import("@/lib/admin/strategy-names");
    const known: ReadonlyArray<string> = KNOWN_STRATEGIES;
    const unknown = allowedStrategies.filter((n) => !known.includes(n));
    if (unknown.length > 0) {
      return Response.json(
        { error: `unknown strategy names: ${unknown.join(", ")}` },
        { status: 400 },
      );
    }
  }

  const updated = await db
    .update(tenants)
    .set({ maxActiveBots, maxActiveStrategies, allowedStrategies })
    .where(eq(tenants.id, id))
    .returning({ id: tenants.id });
  if (updated.length === 0) {
    return Response.json({ error: "tenant_not_found" }, { status: 404 });
  }

  // WHY: only compute bots_over_cap directly here — strategy warnings
  // (strategies_over_cap, active_strategies_outside_allowlist) are
  // deferred until the proxy enforcement PR supplies the
  // running-strategies list per bot.
  const warnings: OverCapWarning[] = [];
  if (maxActiveBots !== null) {
    const rows = await db
      .select({ n: count() })
      .from(tenantBots)
      .where(and(eq(tenantBots.tenantId, id), eq(tenantBots.isRunning, true)));
    const current = Number(rows[0]?.n ?? 0);
    if (current > maxActiveBots) {
      warnings.push({ kind: "bots_over_cap", current, limit: maxActiveBots });
    }
  }

  return Response.json({
    limits: { maxActiveBots, maxActiveStrategies, allowedStrategies },
    warnings,
  });
}
