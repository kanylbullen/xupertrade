/**
 * Lightweight Phase-detection helper. Split out from `phase-sync.ts`
 * so callers that only need to know "is Phase managing auth?" don't
 * pull in `ioredis` (Copilot review fix on PR #104).
 *
 * The route handler at `app/api/auth/config/route.ts` reads this to
 * report `phase_managed` — it has no other reason to touch Redis at
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
 * after trim. Reported as `phase_managed` by GET /api/auth/config
 * (API-only since the Settings auth card was removed in #66). When
 * true, a POST /api/auth/configure edit to a key whose env var is set
 * is overwritten on the next container start.
 *
 * Note: this is the "ANY" predicate by design. If only some env vars
 * are set it still answers true — read it as "Phase is in play, edits
 * are partially shadowed", not "every key is managed". Tailoring it
 * per-key was considered (Copilot suggestion) but rejected as
 * over-engineering: in practice operators set all-or-none.
 */
export function isPhaseManagingAuth(): boolean {
  return PHASE_AUTH_ENV_KEYS.some((key) => {
    const raw = process.env[key];
    return raw != null && String(raw).trim() !== "";
  });
}

/**
 * Returns the list of Phase-managed env var names that ARE set
 * (e.g. ["OIDC_ISSUER", "OIDC_CLIENT_ID"]). No in-app caller since the
 * Settings auth card that enumerated them was removed.
 * Empty array when isPhaseManagingAuth() is false.
 */
export function listPhaseManagedAuthKeys(): string[] {
  return PHASE_AUTH_ENV_KEYS.filter((key) => {
    const raw = process.env[key];
    return raw != null && String(raw).trim() !== "";
  });
}
