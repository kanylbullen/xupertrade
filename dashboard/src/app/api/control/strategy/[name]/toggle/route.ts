/**
 * POST /api/control/strategy/[name]/toggle — enable or disable one
 * strategy on the caller's bot for the requested mode.
 *
 * analysis-2026-09-15 § 5, Medium: `max_active_strategies` and
 * `allowed_strategies` were dead columns. `assertCanEnableStrategy`
 * and `assertStrategyAllowed` had zero call sites, while the admin UI
 * kept offering both as if they did something. This is the one place
 * a tenant turns a strategy ON, so it is where they have to be
 * enforced.
 *
 * Only the enabling direction is gated — a tenant must always be able
 * to turn a strategy off, including when they are already over a cap
 * the operator lowered underneath them.
 */

import {
  assertCanEnableStrategy,
  assertStrategyAllowed,
  LimitExceededError,
  limitExceededResponse,
} from "@/lib/admin/limits";
import { tenantBotFetch } from "@/lib/bot-api";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

type ControlConfig = {
  disabled_strategies?: unknown;
  leverage?: unknown;
};

/**
 * How many strategies are enabled on this bot right now, and is
 * `name` one of them?
 *
 * The bot is the source of truth for live state (`limits.ts` says so
 * explicitly), so we read `/api/control/config` rather than keeping a
 * second copy. `leverage` is keyed by every strategy the bot
 * instantiated; `disabled_strategies` is the subset that is off.
 *
 * Returns null when the response can't be read as that shape. The
 * caller treats that as a refusal, not as "no limit": this whole
 * commit exists because the cap was never applied, and an
 * unrecognised payload is not evidence that it should not be.
 */
function readActiveStrategies(
  data: ControlConfig,
  name: string,
): { activeCount: number; alreadyEnabled: boolean } | null {
  const leverage = data.leverage;
  if (typeof leverage !== "object" || leverage === null) return null;
  const all = Object.keys(leverage as Record<string, unknown>);
  if (all.length === 0) return null;
  const rawDisabled = data.disabled_strategies;
  const disabled = new Set(
    Array.isArray(rawDisabled) ? rawDisabled.map(String) : [],
  );
  return {
    activeCount: all.filter((n) => !disabled.has(n)).length,
    alreadyEnabled: all.includes(name) && !disabled.has(name),
  };
}

export async function POST(
  req: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const body = await req.json().catch(() => ({}));
  // Mirror the bot's own default (`body.get("enabled", True)`) so the
  // gate can't be skipped by omitting the field.
  const enabling = (body as { enabled?: unknown }).enabled !== false;

  if (enabling) {
    let tenant: Awaited<ReturnType<typeof requireTenant>>;
    try {
      tenant = await requireTenant(req);
    } catch (e) {
      if (e instanceof Response) return e;
      throw e;
    }

    try {
      // Allowlist first: exact-match, no round trip. NULL = any
      // strategy allowed, so this is free for most tenants. The bot
      // also enforces it at boot and per tick; doing it here turns a
      // silent bot-side refusal into an answer the UI can render.
      assertStrategyAllowed(tenant, name);

      // The cap needs the live count, so only pay for the extra bot
      // round trip when a cap is actually set.
      if (
        tenant.maxActiveStrategies !== null &&
        tenant.maxActiveStrategies !== undefined
      ) {
        const cfgRes = await tenantBotFetch(req, "/api/control/config");
        if (!cfgRes.ok) {
          // Bot unreachable / no bot for this mode. The toggle itself
          // would fail the same way; forward that answer rather than
          // inventing a different one.
          return cfgRes;
        }
        const parsed = readActiveStrategies(
          (await cfgRes.json().catch(() => ({}))) as ControlConfig,
          name,
        );
        if (parsed === null) {
          return Response.json(
            { error: "strategy-cap-unverifiable" },
            { status: 502 },
          );
        }
        // Re-enabling something already on is a no-op and must not be
        // blocked — it would otherwise trip the cap the moment a
        // tenant sits exactly at it.
        if (!parsed.alreadyEnabled) {
          assertCanEnableStrategy(tenant, parsed.activeCount);
        }
      }
    } catch (e) {
      if (e instanceof LimitExceededError) return limitExceededResponse(e);
      throw e;
    }
  }

  return tenantBotFetch(req, `/api/control/strategy/${encodeURIComponent(name)}/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
