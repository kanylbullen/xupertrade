import { NextResponse } from "next/server";
import { fetchAuthConfig } from "@/lib/auth";
// Import from the detect-only module to avoid pulling ioredis into
// this route's bundle (Copilot review fix on PR #104).
import { isPhaseManagingAuth } from "@/lib/phase-sync-detect";

export const dynamic = "force-dynamic";

export async function GET() {
  // Force-fetch (skip cache) so callers see fresh state. The only
  // in-app caller today is UserMenu, which reads `mode`.
  const cfg = await fetchAuthConfig(true);
  if (cfg === null) {
    // Redis unreachable (fetchAuthConfig returns null) — answer 503
    // rather than silently faking a disabled-auth payload. The error
    // code predates the move from the bot to Redis; kept for callers.
    return NextResponse.json(
      { error: "bot-unreachable" },
      { status: 503 },
    );
  }
  // session_secret is no longer part of the public config response
  // (auth.ts → getSessionSecret fetches it from a separate API_KEY-gated
  // endpoint instead). Callers only need these fields.
  return NextResponse.json({
    mode: cfg.mode,
    basic_user_set: cfg.basic_user_set,
    oidc_issuer: cfg.oidc_issuer,
    oidc_client_id: cfg.oidc_client_id,
    oidc_scopes: cfg.oidc_scopes,
    // True when any Phase auth env var is set. At every start
    // `src/instrumentation.ts` copies each set var into its Redis key,
    // so a POST /api/auth/configure edit to one of those keys does
    // not survive a restart (see lib/phase-sync.ts for which keys).
    phase_managed: isPhaseManagingAuth(),
  });
}
