/**
 * Next.js instrumentation hook — runs once per server process at start.
 *
 * Today: syncs operator-managed Phase env vars into Redis so dashboard
 * code paths that read auth-config straight from Redis (notably the
 * OIDC callback) can't drift from Phase after an operator rotation.
 * See `lib/phase-sync.ts` for the full incident context (2026-05-13).
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
}
