import { NextResponse } from "next/server";
import { fetchAuthConfig } from "@/lib/auth";
import { isPhaseManagingAuth } from "@/lib/phase-sync";

export const dynamic = "force-dynamic";

export async function GET() {
  // Force-fetch (skip cache) so the Options page sees fresh state
  const cfg = await fetchAuthConfig(true);
  if (cfg === null) {
    // Bot unreachable — surface that to the Options page rather than
    // silently faking a disabled-auth payload.
    return NextResponse.json(
      { error: "bot-unreachable" },
      { status: 503 },
    );
  }
  // session_secret is no longer part of the public config response
  // (auth.ts → getSessionSecret fetches it from a separate API_KEY-gated
  // endpoint instead). The Options page only needs these fields.
  return NextResponse.json({
    mode: cfg.mode,
    basic_user_set: cfg.basic_user_set,
    oidc_issuer: cfg.oidc_issuer,
    oidc_client_id: cfg.oidc_client_id,
    oidc_scopes: cfg.oidc_scopes,
    // When true, `src/instrumentation.ts` overwrites these Redis keys
    // from Phase env at every container start — UI edits will not
    // survive a restart. Drives the banner in `auth-config.tsx`.
    phase_managed: isPhaseManagingAuth(),
  });
}
