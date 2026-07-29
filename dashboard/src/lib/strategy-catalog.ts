import "server-only";

import { and, eq } from "drizzle-orm";

import { getBotApiUrl } from "./bot-api";
import { loadBotApiKey } from "./bot-api-key";
import { db, tenantBots } from "./db";

/**
 * A strategy as the bot reports it: live identity plus whatever prose
 * lives in `bot/hypertrade/strategies/meta/<name>.json`.
 *
 * Everything except `name` is optional — a strategy with no metadata
 * file still lists, it just has no documentation yet. That keeps adding
 * a strategy from being blocked on writing docs for it.
 */
export type CatalogStrategy = {
  name: string;
  symbol?: string;
  timeframe?: string;
  tvUrl?: string | null;
  summary?: string;
  logic?: string[];
  strengths?: string[];
  weaknesses?: string[];
  params?: Record<string, string | number | boolean>;
  stats?: {
    apr?: string;
    sharpe?: string;
    maxDrawdown?: string;
    winRate?: string;
    trades?: string;
  };
};

/**
 * Fetch the strategy catalogue from any of this tenant's running bots.
 *
 * The page is mode-agnostic — it documents what the code can do, not
 * what one bot is currently doing — so any running bot answers equally
 * well. Modes are tried mainnet-first only to match the ordering used
 * elsewhere in the UI; the response does not differ by mode.
 *
 * Returns an empty array when the tenant has no running bot. The caller
 * renders an empty state; this deliberately does NOT fall back to a
 * bundled copy of the catalogue, because a stale hardcoded duplicate
 * drifting from the code is the exact problem this replaced.
 */
export async function fetchStrategyCatalog(
  tenantId: string,
): Promise<CatalogStrategy[]> {
  const rows = await db
    .select()
    .from(tenantBots)
    .where(and(eq(tenantBots.tenantId, tenantId), eq(tenantBots.isRunning, true)));

  const order = { mainnet: 0, testnet: 1, paper: 2 } as Record<string, number>;
  const candidates = [...rows].sort(
    (a, b) => (order[a.mode] ?? 99) - (order[b.mode] ?? 99),
  );

  for (const row of candidates) {
    const base = getBotApiUrl(row);
    if (!base) continue;
    try {
      const apiKey = await loadBotApiKey(row.id);
      const res = await fetch(`${base}/strategies`, {
        headers: apiKey ? { "X-Api-Key": apiKey } : {},
        // Never cache: the catalogue reflects what this bot has
        // registered, which changes when the operator edits the
        // allowlist and the bot restarts.
        cache: "no-store",
        signal: AbortSignal.timeout(5000),
      });
      if (!res.ok) continue;
      const body = (await res.json()) as { strategies?: CatalogStrategy[] };
      if (Array.isArray(body.strategies) && body.strategies.length > 0) {
        return body.strategies;
      }
    } catch {
      // Bot unreachable or slow — try the next one.
    }
  }
  return [];
}
