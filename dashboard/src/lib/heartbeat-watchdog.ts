/**
 * Heartbeat watchdog (feat/heartbeat-watchdog).
 *
 * Why this exists: 2026-05-25 → 05-27 all three tenant bots were
 * silently offline for ~2.5 days and NO Telegram alert fired. The
 * operator found out by chance. The dashboard's bot-status-indicator is
 * client-side only — it just paints a red dot when someone has the page
 * open, it never alerts. This is the worst failure mode (silent
 * downtime). This watchdog is an EXTERNAL poller that runs in the
 * long-lived dashboard Node process and pushes a Telegram alert the
 * moment a bot's heartbeat goes stale.
 *
 * How: every poll interval, for each `tenant_bots` row with
 * `is_running = true`, fetch the bot's `GET /api/heartbeat`. A bot is
 * "down" when the fetch fails, OR the bot reports `stale = true`, OR
 * `age_seconds` exceeds the staleness threshold. The bot writes its
 * heartbeat every ~60s tick (stale at 180s); the default 5-minute
 * threshold means a bot is solidly dead, not just one slow tick — avoids
 * flapping.
 *
 * Scope / what this DOES and does NOT catch (important — read this):
 *   - DOES catch a bot that crashed or stalled while the DB still
 *     believes it's running. This is the failure mode behind the
 *     2026-05-25 outage: the three bots stayed `is_running = true` the
 *     whole time (`last_stopped_at` was NULL — nobody stopped them), the
 *     containers died, and the heartbeat went stale / unreachable. A
 *     crashed container keeps `is_running = true` until someone
 *     explicitly stops it, so the fetch-failure path below flags exactly
 *     this case. THIS is the gap the PR closes.
 *   - Does NOT catch a bot an operator simply forgot to start. The DB is
 *     the source of truth for INTENT: `is_running = false` means
 *     "intentionally stopped" from the system's point of view, and the
 *     watchdog deliberately does not second-guess that. Alerting on
 *     `is_running = false` rows would fire constantly for every
 *     deliberately-stopped bot. Detecting "should be running but isn't"
 *     would need a separate desired-state signal we don't have today.
 *
 * Detection latency: worst case is one full poll interval plus the
 * staleness threshold (default 60s + 300s = ~6 min) between a bot going
 * silent and the alert firing. Both knobs are env-tunable — see
 * `POLL_INTERVAL_MS` / `DOWN_AGE_SECONDS` below.
 *
 * Alerting is operator-level via `sendOperatorTelegramAlert` (the
 * operator's Phase-sourced token), NOT the bot's own notifier — a dead
 * bot can't alert about its own death. Per-tenant alerting is a future
 * extension; see telegram-alert.ts.
 *
 * Dedup: a Redis key `dashboard:heartbeat-alert:<botId>` (TTL 24h)
 * records that we've already alerted for this bot, so we send exactly
 * one "down" alert per outage and one "recovered" alert when it comes
 * back — no per-minute spam.
 *
 * Fail-safe: every per-bot check is wrapped in try/catch; one bot's
 * failure never kills the loop, and the loop itself never throws out of
 * the interval callback. The watchdog must never crash the dashboard.
 *
 * Server-only — uses ioredis + the DB. Started from
 * `src/instrumentation.ts` behind the `NEXT_RUNTIME === "nodejs"` fence.
 */
import "server-only";

import { eq } from "drizzle-orm";
import type { Redis } from "ioredis";

import { loadBotApiKey } from "./bot-api-key";
import { getBotApiUrl } from "./bot-api";
import { db, tenantBots } from "./db";
import { getRedisClient } from "./redis";
import { escapeTelegramHtml, sendOperatorTelegramAlert } from "./telegram-alert";

/**
 * Read a positive-integer env var, falling back to `fallback` when unset,
 * empty, non-numeric, or non-positive. Keeps the watchdog fail-safe: a
 * fat-fingered env value degrades to the documented default rather than
 * disabling polling (0) or going negative.
 */
function envPositiveInt(name: string, fallback: number): number {
  const raw = (process.env[name] ?? "").trim();
  if (!raw) return fallback;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.floor(n);
}

/** Poll interval (seconds). Override via HEARTBEAT_WATCHDOG_POLL_SECONDS. */
const POLL_INTERVAL_SECONDS = envPositiveInt(
  "HEARTBEAT_WATCHDOG_POLL_SECONDS",
  60,
);
const POLL_INTERVAL_MS = POLL_INTERVAL_SECONDS * 1000;
/**
 * Down when the bot's last heartbeat is older than this (seconds).
 * Override via HEARTBEAT_WATCHDOG_STALE_SECONDS.
 */
const DOWN_AGE_SECONDS = envPositiveInt(
  "HEARTBEAT_WATCHDOG_STALE_SECONDS",
  300,
);
/** Per-bot heartbeat fetch timeout. */
const FETCH_TIMEOUT_MS = 8_000;
/** Dedup key TTL: re-alert if the same outage somehow persists 24h. */
const DEDUP_TTL_SECONDS = 24 * 60 * 60;

/**
 * Send an alert; resolves to `true` on a confirmed successful send,
 * `false` otherwise. Never throws (see telegram-alert.ts). The recovery
 * path relies on the boolean to decide whether to clear the dedup key.
 */
type SendAlert = (text: string) => Promise<boolean>;

function dedupKey(botId: string): string {
  return `dashboard:heartbeat-alert:${botId}`;
}

type HeartbeatProbe = {
  /** True when we should treat the bot as down (alertable). */
  down: boolean;
  /** age_seconds the bot reported, or null when unknown (fetch failed / no heartbeat). */
  ageSeconds: number | null;
  /**
   * True when we COULDN'T probe (e.g. no API key) and must not draw any
   * conclusion. The sweep skips skipped bots entirely — no down alert,
   * no recovery, no dedup-key mutation. Distinguishes "couldn't probe"
   * from "probed and confirmed down".
   */
  skip?: boolean;
};

/**
 * Probe one bot's heartbeat endpoint. Never throws — any failure
 * (no URL, fetch error, non-2xx) is treated as "down" with unknown age.
 *
 * Exception: a missing API key is NOT treated as down. Probing without
 * the key would fail auth and produce a FALSE offline alert, but the
 * real problem is auth-infra (key evicted / legacy bot predating per-bot
 * keys), not a confirmed outage. Such a bot is returned with `skip:true`
 * so the sweep leaves its alert state untouched.
 */
async function probeBot(
  row: typeof tenantBots.$inferSelect,
): Promise<HeartbeatProbe> {
  const base = getBotApiUrl(row);
  if (!base) return { down: true, ageSeconds: null };

  const apiKey = await loadBotApiKey(row.id);
  if (!apiKey) {
    return { down: false, ageSeconds: null, skip: true };
  }

  try {
    const res = await fetch(`${base}/api/heartbeat`, {
      cache: "no-store",
      headers: { "X-Api-Key": apiKey },
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!res.ok) return { down: true, ageSeconds: null };
    const data = (await res.json()) as {
      stale?: boolean;
      age_seconds?: number | null;
    };
    const age =
      typeof data.age_seconds === "number" ? data.age_seconds : null;
    const down =
      data.stale === true || age === null || age > DOWN_AGE_SECONDS;
    return { down, ageSeconds: age };
  } catch {
    // Connection refused / DNS fail / timeout / bad JSON → down.
    return { down: true, ageSeconds: null };
  }
}

/**
 * Run one full sweep over all running bots. Exported for tests. Pass
 * explicit `redis` / `sendAlert` to inject mocks; production callers use
 * the defaults.
 */
export async function runHeartbeatSweep(
  redis: Redis = getRedisClient(),
  sendAlert: SendAlert = sendOperatorTelegramAlert,
): Promise<void> {
  const rows = await db
    .select()
    .from(tenantBots)
    .where(eq(tenantBots.isRunning, true));

  for (const row of rows) {
    try {
      const { down, ageSeconds, skip } = await probeBot(row);
      if (skip) {
        // Couldn't probe (e.g. missing API key) — draw no conclusion and
        // leave alert state untouched. A skip is an auth-infra problem,
        // not a confirmed outage; alerting here would be a false alarm.
        console.warn(
          `[heartbeat-watchdog] skipped bot=${row.id} mode=${row.mode}: ` +
            "no API key — cannot probe (not treated as down)",
        );
        continue;
      }
      const key = dedupKey(row.id);
      const alreadyAlerted = (await redis.get(key)) !== null;

      if (down && !alreadyAlerted) {
        const mode = escapeTelegramHtml(row.mode);
        const tenant = escapeTelegramHtml(row.tenantId);
        const ageText =
          ageSeconds === null ? "unknown" : `${ageSeconds}s`;
        await sendAlert(
          `🔴 Bot <b>${mode}</b> (${tenant}) offline — last heartbeat ${ageText} ago`,
        );
        // Set the dedup key even if the send couldn't be confirmed: the
        // bot IS down, so a missed first alert is preferable to spamming
        // the same down-alert every poll. The 24h TTL re-alerts if the
        // outage somehow persists. (Recovery is the asymmetric case — see
        // below — because losing a recovery notice leaves stale state.)
        await redis.set(key, "alerted", "EX", DEDUP_TTL_SECONDS);
        console.warn(
          `[heartbeat-watchdog] alerted DOWN bot=${row.id} mode=${row.mode} age=${ageText}`,
        );
      } else if (!down && alreadyAlerted) {
        const mode = escapeTelegramHtml(row.mode);
        const tenant = escapeTelegramHtml(row.tenantId);
        const sent = await sendAlert(
          `🟢 Bot <b>${mode}</b> (${tenant}) recovered`,
        );
        if (sent) {
          // Only clear the dedup key once the recovery alert is confirmed
          // delivered. If the send failed, leave the key in place so the
          // next sweep retries the recovery alert rather than silently
          // dropping it and leaving state inconsistent.
          await redis.del(key);
          console.log(
            `[heartbeat-watchdog] alerted RECOVERY bot=${row.id} mode=${row.mode}`,
          );
        } else {
          console.warn(
            `[heartbeat-watchdog] recovery send failed bot=${row.id} mode=${row.mode} — ` +
              "keeping dedup key, will retry next sweep",
          );
        }
      }
    } catch (err) {
      // One bot's check must never abort the sweep over the others.
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(
        `[heartbeat-watchdog] check failed bot=${row.id} mode=${row.mode}: ${msg}`,
      );
    }
  }
}

let started = false;

/**
 * Start the recurring heartbeat watchdog. Idempotent — guarded by a
 * module-level boolean because Next's `register()` can in principle run
 * more than once per process. The interval callback swallows all errors
 * so a transient DB/Redis outage logs and retries on the next tick
 * rather than crashing the dashboard.
 */
export function startHeartbeatWatchdog(): void {
  if (started) return;
  started = true;

  console.log(
    `[heartbeat-watchdog] started — polling every ${POLL_INTERVAL_MS / 1000}s, ` +
      `down threshold ${DOWN_AGE_SECONDS}s`,
  );

  const tick = () => {
    runHeartbeatSweep().catch((err) => {
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`[heartbeat-watchdog] sweep failed: ${msg}`);
    });
  };

  const timer = setInterval(tick, POLL_INTERVAL_MS);
  // Don't keep the event loop alive solely for the watchdog — the
  // dashboard server already holds it open; this just avoids blocking a
  // clean process exit in tooling/tests.
  if (typeof timer.unref === "function") timer.unref();

  // Kick off an immediate first sweep so a bot that's already down at
  // boot gets flagged within seconds, not after the first 60s interval.
  tick();
}

/** Test-only: reset the module-level start guard. */
export function _resetWatchdogStartedForTests(): void {
  started = false;
}
