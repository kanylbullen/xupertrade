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
import { checkRateLimit } from "@/lib/rate-limit";

export const dynamic = "force-dynamic";

// H-2: cap OIDC authorization-request initiations per IP. Each call
// mints a state cookie and 302s to the upstream IdP; an attacker can
// otherwise spam this endpoint to fill the IdP's state cache or fill
// our access logs. Limit is human-paced (60/min is well above any
// legit retry pattern but cheap to enforce).
const OIDC_START_RATE_LIMIT_MAX = 60;
const OIDC_START_RATE_LIMIT_WINDOW_SEC = 60;

function getClientIp(req: Request): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const first = xff.split(",")[0]?.trim();
    if (first) return first;
  }
  return "unknown";
}

export async function GET(req: Request) {
  const url = new URL(req.url);
  const next = safeNext(url.searchParams.get("next") || "/");

  const ip = getClientIp(req);
  const rl = await checkRateLimit(
    "auth-oidc-start",
    ip,
    OIDC_START_RATE_LIMIT_MAX,
    OIDC_START_RATE_LIMIT_WINDOW_SEC,
  );
  if (!rl.allowed) {
    return NextResponse.json(
      { error: "rate-limited", retry_after_seconds: rl.resetInSeconds },
      {
        status: 429,
        headers: { "Retry-After": String(rl.resetInSeconds) },
      },
    );
  }

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
