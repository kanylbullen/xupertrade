/**
 * The start-slot claim on a `tenant_bots` row.
 *
 * Both start routes reserve a start by writing `is_running = true` with
 * this placeholder as `container_id` and `last_started_at = now()`,
 * inside `reserveBotStart`'s locked transaction, so the operator's
 * `max_active_bots` count sees the start from the moment it passes (see
 * `lib/admin/limits.ts`). `decryptAndStart` replaces the placeholder
 * with the real container id once the container is up; the routes undo
 * the claim on any failure, thrown or returned.
 *
 * A claim is transient by design. Two things treat it specially:
 *
 *  - GET /api/tenant/me/bots/[id] does not reconcile a claim against
 *    Docker unless it is STALE: there is no container named
 *    "claiming", so the 404 would flip the row to not-running
 *    mid-start and hand the cap slot to a concurrent start.
 *  - `reserveBotStart` reaps STALE claims — ones whose request died
 *    mid-start (dashboard restart, crash) — so they can't hold a cap
 *    slot forever.
 *
 * "Stale" requires a timestamp. A claim with no `last_started_at`
 * can't be aged, so it is never reaped or reconciled away: reaping it
 * would let a concurrent start take the slot of a start that is still
 * running (the integration test caught exactly that). Stop clears it.
 */

/** `container_id` value while a start holds the slot. */
export const CLAIM_PLACEHOLDER = "claiming";

/**
 * Age after which a claim is presumed abandoned. A real start is
 * Argon2id + a role check + `docker create/start` — seconds. Ten
 * minutes is far past any live request, and short enough that a
 * capped tenant isn't blocked for long by one that died.
 */
export const CLAIM_STALE_AFTER_SECONDS = 10 * 60;

type ClaimRow = { containerId: string | null; lastStartedAt: Date | null };

/** The row holds the start-slot claim placeholder. */
export function isClaim(row: ClaimRow): boolean {
  return row.containerId === CLAIM_PLACEHOLDER;
}

/** A claim at least CLAIM_STALE_AFTER_SECONDS old. False for a claim
 *  with no timestamp (can't be aged) and for any non-claim row. */
export function isStaleClaim(row: ClaimRow, nowMs: number = Date.now()): boolean {
  if (!isClaim(row) || row.lastStartedAt === null) return false;
  return nowMs - row.lastStartedAt.getTime() >= CLAIM_STALE_AFTER_SECONDS * 1000;
}
