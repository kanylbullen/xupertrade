/**
 * Next.js instrumentation hook — runs once per server process at start.
 *
 * Today:
 *  - syncs operator-managed Phase env vars into Redis so dashboard
 *    code paths that read auth-config straight from Redis (notably the
 *    OIDC callback) can't drift from Phase after an operator rotation.
 *    See `lib/phase-sync.ts` for the full incident context (2026-05-13).
 *  - warns when OIDC sign-in is configured but `OIDC_REQUIRED_GROUP`
 *    is empty, i.e. every user the IdP authenticates gets a tenant.
 *    See `lib/oidc-group-gate.ts` (roadmap NU-8 point 2).
 *  - starts the heartbeat watchdog: an external poller that alerts via
 *    Telegram when a running tenant bot goes silent. See
 *    `lib/heartbeat-watchdog.ts` for the 2026-05-25→27 silent-outage
 *    context that motivated it.
 *
 * The hook is loaded in BOTH the server and (if configured) edge/client
 * runtimes. ioredis is a Node-only module — bundling it client-side
 * would break the build. The `NEXT_RUNTIME === "nodejs"` fence is the
 * documented way to keep imports server-only inside `register()`.
 * https://nextjs.org/docs/app/api-reference/file-conventions/instrumentation
 */
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;

  // Dynamic import — keeps ioredis out of the edge bundle entirely.
  // The static-analysis pass that decides what to ship to non-node
  // runtimes doesn't follow this path.
  const { syncPhaseAuthConfig } = await import("./lib/phase-sync");
  await syncPhaseAuthConfig();

  // After the sync, so the mode it reads is the one Phase just wrote.
  // Not awaited: with Redis down at boot the config read waits out
  // ioredis's retries, and a log line must not hold up the server. It
  // never rejects (a failed read still warns).
  const { warnIfOidcGroupGateOff } = await import("./lib/oidc-group-gate");
  void warnIfOidcGroupGateOff();

  // Long-running poller — fire-and-forget. startHeartbeatWatchdog is
  // idempotent (module-level guard) and its interval callback swallows
  // all errors, so a register() re-run or a transient outage can't
  // crash the dashboard.
  const { startHeartbeatWatchdog } = await import("./lib/heartbeat-watchdog");
  startHeartbeatWatchdog();
}
