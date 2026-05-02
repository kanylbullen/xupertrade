import { NextResponse } from "next/server";
import * as client from "openid-client";
import {
  getOidcConfig,
  encodeStateBundle,
  STATE_COOKIE,
  STATE_COOKIE_OPTIONS,
  safeNext,
  resolveRedirectUri,
} from "@/lib/oidc";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const next = safeNext(url.searchParams.get("next") || "/");

  const oidc = await getOidcConfig();
  if (!oidc) {
    const publicBase =
      (process.env.PUBLIC_URL || process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, "");
    const errorUrl = publicBase
      ? new URL("/login?error=oidc-misconfigured", publicBase + "/")
      : new URL("/login?error=oidc-misconfigured", url);
    return NextResponse.redirect(errorUrl);
  }
  const { config, cfg } = oidc;

  const code_verifier = client.randomPKCECodeVerifier();
  const code_challenge = await client.calculatePKCECodeChallenge(code_verifier);
  const state = client.randomState();

  // Build redirect_uri from PUBLIC_URL/DASHBOARD_URL env if set, else
  // request origin. Required for containerized prod where req.url returns
  // the internal docker hostname.
  const redirect_uri = resolveRedirectUri(req);

  const authUrl = client.buildAuthorizationUrl(config, {
    redirect_uri,
    scope: cfg.oidc_scopes || "openid profile email",
    state,
    code_challenge,
    code_challenge_method: "S256",
  });

  const res = NextResponse.redirect(authUrl);
  res.cookies.set(
    STATE_COOKIE,
    encodeStateBundle({ code_verifier, state, next }),
    STATE_COOKIE_OPTIONS,
  );
  return res;
}
