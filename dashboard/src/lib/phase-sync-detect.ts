/**
 * Lightweight Phase-detection helper. Split out from `phase-sync.ts`
 * so callers that only need to know "is Phase managing auth?" don't
 * pull in `ioredis` (Copilot review fix on PR #104).
 *
 * The route handler at `app/api/auth/config/route.ts` reads this to
 * power the UI banner — it has no other reason to touch Redis at
 * import time, and dragging ioredis into that bundle inflates the
 * server route's cold-start size.
 *
 * Server-only: reads `process.env`. Safe to import from anywhere
 * except a Client Component.
 */
import "server-only";

/** Same env-var list as `phase-sync.ts:SYNC_KEYS`. Keep in lockstep. */
const PHASE_AUTH_ENV_KEYS = [
  "OIDC_ISSUER",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_SCOPES",
  "AUTH_MODE",
] as const;

/**
 * Returns true if ANY of the Phase-managed auth env vars is non-empty
 * after trim. Used by the Settings → Authentication banner: when
 * true, edits via the UI will be overwritten on next container start.
 *
 * Note: this is the "ANY" predicate by design. If only some env vars
 * are set, the UI banner still warns — the operator should mentally
 * model "Phase is in play, edits are partially-shadowed". Tailoring
 * the banner per-key was considered (Copilot suggestion) but rejected
 * as over-engineering: in practice operators set all-or-none.
 */
export function isPhaseManagingAuth(): boolean {
  return PHASE_AUTH_ENV_KEYS.some((key) => {
    const raw = process.env[key];
    return raw != null && String(raw).trim() !== "";
  });
}

/**
 * Returns the list of Phase-managed env var names that ARE set, for
 * the UI banner to enumerate ("Phase manages: OIDC_ISSUER, OIDC_CLIENT_ID").
 * Empty array when isPhaseManagingAuth() is false.
 */
export function listPhaseManagedAuthKeys(): string[] {
  return PHASE_AUTH_ENV_KEYS.filter((key) => {
    const raw = process.env[key];
    return raw != null && String(raw).trim() !== "";
  });
}
