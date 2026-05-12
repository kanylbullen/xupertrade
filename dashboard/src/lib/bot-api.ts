import { and, eq } from "drizzle-orm";

import { API_PORT_BY_MODE, isValidMode, type BotMode } from "./bot-orchestrator";
import { db, tenantBots } from "./db";
import { requireTenant } from "./tenant";

export type Mode = BotMode;

const URLS: Record<Mode, string> = {
  paper: process.env.BOT_API_URL_PAPER || "http://localhost:8000",
  testnet: process.env.BOT_API_URL_TESTNET || "http://localhost:8001",
  mainnet: process.env.BOT_API_URL_MAINNET || "http://localhost:8002",
};

export function botUrl(mode: Mode): string {
  return URLS[mode];
}

export const BOT_API_URL = URLS.testnet; // back-compat for older imports

/**
 * Build the dashboard→bot HTTP URL from a `tenant_bots` row. Replaces
 * the env-driven `botUrl(mode)` for tenant-aware routes (Phase 6c
 * PR ε will wire the existing routes to use this).
 *
 * Convention:
 *   - host = container_name (docker DNS resolves it inside the
 *     compose network for both operator's compose-defined bots and
 *     orchestrator-spawned tenant bots)
 *   - port = API_PORT_BY_MODE[mode] (single source of truth in
 *     bot-orchestrator.ts; orchestrator injects API_PORT for new
 *     tenant bots so they match operator's compose convention)
 *
 * Returns `null` if the row has no `containerName` (bot is provisioned
 * in DB but the orchestrator hasn't actually started a container yet
 * — caller should treat this as "no bot for this mode" → 404 to UI).
 */
export function getBotApiUrl(
  row: typeof tenantBots.$inferSelect,
): string | null {
  if (!row.containerName) return null;
  // isValidMode is the canonical mode validator (bot-orchestrator.ts
  // owns the BotMode union). Reuse rather than duplicate the literal
  // list here so the two stay in lockstep.
  if (!isValidMode(row.mode)) return null;
  return `http://${row.containerName}:${API_PORT_BY_MODE[row.mode as BotMode]}`;
}

function parseMode(req: Request): Mode {
  const url = new URL(req.url);
  const m = url.searchParams.get("mode");
  return m === "paper" || m === "mainnet" ? m : "testnet";
}

/**
 * Internal: do the actual fetch with API_KEY forwarding + standard
 * error mapping. Shared by `botFetch` (env-URL legacy path) and
 * `tenantBotFetch` (tenant_bots-lookup path).
 */
async function _doBotFetch(
  base: string,
  path: string,
  mode: Mode,
  init?: RequestInit,
): Promise<Response> {
  // Forward the dashboard's API_KEY as X-Api-Key so the bot's
  // _require_auth gate accepts our control-route POSTs (pause, flat-all,
  // strategy toggle, leverage, tls/configure, auth/configure, ...).
  // Two failure modes if we DON'T send it:
  //   • API_KEY set on the bot → those routes return 401 and the
  //     dashboard buttons silently break.
  //   • API_KEY empty on the bot (the .env.example default) → auth is
  //     globally disabled bot-side and anyone reachable to the bot's
  //     host-bound port can hit those endpoints unauthenticated.
  // Forwarding is harmless in both cases (no-op when the bot has no
  // API_KEY) and makes the gate actually effective once API_KEY is set.
  //
  // Caller-supplied headers in `init.headers` take precedence so a
  // route can override (e.g. for endpoints that explicitly require a
  // different auth scheme).
  const apiKey = process.env.API_KEY || "";
  const baseHeaders: HeadersInit = apiKey ? { "X-Api-Key": apiKey } : {};
  const headers = init?.headers
    ? { ...baseHeaders, ...Object.fromEntries(new Headers(init.headers)) }
    : baseHeaders;

  try {
    const res = await fetch(`${base}${path}`, {
      ...init,
      cache: "no-store",
      headers,
    });

    // Pass through both 2xx and 4xx so the dashboard surfaces actionable
    // bot-side errors verbatim (e.g. 400 invalid leverage, 401 API_KEY
    // mismatch, 403 strategy disabled). Bot 5xx is squashed to 502 to
    // distinguish "bot misbehaved" from "the dashboard misbehaved".
    //
    // Body parsing is best-effort: most bot endpoints return JSON, but
    // a few error paths (or non-error endpoints) return text. Forward
    // whichever we got rather than choking on parse failure.
    if (res.status >= 500) {
      const body = await res.text().catch(() => "");
      return Response.json(
        { error: `Bot API returned ${res.status}`, detail: body.slice(0, 500) },
        { status: 502 },
      );
    }
    const contentType = res.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const data = await res.json().catch(() => ({}));
      return Response.json(data, { status: res.status });
    }
    const text = await res.text().catch(() => "");
    return new Response(text, {
      status: res.status,
      headers: contentType ? { "content-type": contentType } : undefined,
    });
  } catch {
    return Response.json(
      { error: `Bot API at ${base} unreachable (mode=${mode})` },
      { status: 502 }
    );
  }
}

/**
 * Legacy env-URL proxy. Used by routes that haven't been migrated to
 * `tenantBotFetch` yet. After Phase 6c PR ε this is only retained for
 * back-compat — no production code path should depend on it.
 */
export async function botFetch(req: Request, path: string, init?: RequestInit) {
  const mode = parseMode(req);
  const base = botUrl(mode);
  return _doBotFetch(base, path, mode, init);
}

/**
 * Tenant-aware proxy (Phase 6c PR ε). Resolves the calling tenant via
 * `requireTenant`, looks up their bot for the requested mode in
 * `tenant_bots`, and proxies to that bot's URL. Returns 401 (no
 * session), 404 (no bot for this mode), or 502 (bot unreachable).
 *
 * Operator gets routed to their existing 3 bots via the rows Phase 6b
 * inserted into tenant_bots — no special-case code path needed.
 */
export async function tenantBotFetch(
  req: Request,
  path: string,
  init?: RequestInit,
): Promise<Response> {
  // Resolve tenant — throws Response (401) if no/invalid session.
  let tenantId: string;
  try {
    const t = await requireTenant(req);
    tenantId = t.id;
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const mode = parseMode(req);

  const rows = await db
    .select()
    .from(tenantBots)
    .where(
      and(
        eq(tenantBots.tenantId, tenantId),
        eq(tenantBots.mode, mode),
        // Only route to bots whose DB row says is_running=true.
        // Without this, a stale row (e.g. container manually
        // `docker rm`'d outside our /stop endpoint, or a host-
        // reboot that left is_running=true but no container) would
        // make every poll wait 4s for a connection refused, return
        // 502, and spam the console. The /stop endpoint clears
        // both is_running and container_id on a clean stop, so
        // this filter only excludes truly-broken state — clean
        // stops + creates are unaffected.
        eq(tenantBots.isRunning, true),
      ),
    )
    .limit(1);
  if (rows.length === 0) {
    return Response.json(
      { error: `no running ${mode} bot for tenant`, mode },
      { status: 404 },
    );
  }

  const base = getBotApiUrl(rows[0]);
  if (!base) {
    return Response.json(
      { error: `tenant ${mode} bot not started`, mode },
      { status: 404 },
    );
  }

  return _doBotFetch(base, path, mode, init);
}
