import { NextResponse } from "next/server";
import { fetchAuthConfig } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET() {
  // Force-fetch (skip cache) so the Options page sees fresh state
  const cfg = await fetchAuthConfig(true);
  // session_secret is no longer part of the public config response
  // (auth.ts → getSessionSecret fetches it from a separate API_KEY-gated
  // endpoint instead). The Options page only needs these fields.
  return NextResponse.json({
    mode: cfg.mode,
    basic_user_set: cfg.basic_user_set,
    oidc_issuer: cfg.oidc_issuer,
    oidc_client_id: cfg.oidc_client_id,
    oidc_scopes: cfg.oidc_scopes,
  });
}
