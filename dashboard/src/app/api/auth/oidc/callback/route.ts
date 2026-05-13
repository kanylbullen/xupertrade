import { NextResponse } from "next/server";
import * as client from "openid-client";
import {
  getOidcConfig,
  decodeStateBundle,
  STATE_COOKIE,
  safeNext,
  resolveRedirectUri,
} from "@/lib/oidc";
import {
  COOKIE_OPTIONS,
  getSessionSecret,
  newSessionPayload,
  signSession,
} from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);

  // Recover the bundle the start route stashed
  const cookieHeader = req.headers.get("cookie") || "";
  const stateCookie = cookieHeader
    .split(";")
    .map((p) => p.trim())
    .find((p) => p.startsWith(`${STATE_COOKIE}=`))
    ?.split("=", 2)[1];
  if (!stateCookie) {
    return loginError(url, "oidc-state-missing");
  }
  const bundle = decodeStateBundle(decodeURIComponent(stateCookie));
  if (!bundle) return loginError(url, "oidc-state-invalid");

  const oidc = await getOidcConfig();
  if (!oidc) return loginError(url, "oidc-misconfigured");
  const { config } = oidc;

  // Reconstruct the callback URL using the SAME origin we used at auth
  // time (PUBLIC_URL/DASHBOARD_URL if set). openid-client uses this URL
  // both to read code+state AND to derive the redirect_uri sent to the
  // token endpoint — provider will reject mismatched redirect_uri.
  const publicCallback = new URL(resolveRedirectUri(req));
  // Carry through query params (code, state, ...) from the actual request
  url.searchParams.forEach((v, k) => publicCallback.searchParams.set(k, v));

  let tokens;
  try {
    tokens = await client.authorizationCodeGrant(config, publicCallback, {
      expectedState: bundle.state,
      pkceCodeVerifier: bundle.code_verifier,
      idTokenExpected: true,
    });
  } catch (e) {
    console.error("OIDC token exchange failed:", e);
    return loginError(url, "oidc-token-exchange-failed");
  }

  const claims = tokens.claims();
  if (!claims) return loginError(url, "oidc-no-claims");

  // Use email > preferred_username > sub as the session subject, in that
  // order — what the user thinks of as "their identity" with the provider.
  const sub = String(
    claims.email ||
      claims.preferred_username ||
      claims.sub ||
      "oidc-user",
  );

  // M-3: extract `groups` claim. Authentik exposes this natively as a
  // JSON array of group names when the OIDC provider's scope includes
  // it (default scope mapping). Some IdPs may emit a single string or
  // omit the claim entirely — accept both shapes, ignore the rest.
  // The session payload will use this in the tenant resolver to gate
  // autocreate against `OIDC_REQUIRED_GROUP`.
  const groups = extractGroupsClaim(claims.groups);

  // Sign the session cookie. Secret comes from the API_KEY-gated bot
  // endpoint so it's never publicly exposed. Fail closed if unavailable.
  const secret = await getSessionSecret(true);
  if (!secret) {
    return loginError(url, "oidc-session-secret-unavailable");
  }
  const sessionValue = signSession(newSessionPayload(sub, groups), secret);

  // Use PUBLIC_URL (or DASHBOARD_URL) as the base for the post-login
  // redirect so users land on the public hostname, not the docker
  // container hostname embedded in req.url.
  const target = safeNext(bundle.next);
  const publicBase =
    (process.env.PUBLIC_URL || process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, "");
  const redirectUrl = publicBase
    ? new URL(target, publicBase + "/")
    : new URL(target, url);
  const res = NextResponse.redirect(redirectUrl);
  res.cookies.set(COOKIE_OPTIONS.name, sessionValue, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: COOKIE_OPTIONS.maxAge,
  });
  // Clear the state cookie — single-use
  res.cookies.set(STATE_COOKIE, "", {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return res;
}

function publicLoginUrl(reqUrl: URL): URL {
  const publicBase =
    (process.env.PUBLIC_URL || process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, "");
  return publicBase
    ? new URL("/login", publicBase + "/")
    : new URL("/login", reqUrl);
}

/**
 * Normalize an OIDC `groups` claim into a string[] or undefined.
 * Authentik returns a JSON array; some other IdPs return a single
 * string. We accept exactly two shapes:
 *
 *   - JSON array of strings → kept as-is, non-string entries dropped
 *   - single non-empty string → wrapped in `[raw]` (treated as ONE
 *     group; we do NOT split on commas/spaces because both characters
 *     are legal inside group names and splitting would silently turn
 *     "ops, admins" into two membership lookups)
 *
 * Anything else → undefined (treated as "no groups" downstream — fine
 * because group enforcement is opt-in via OIDC_REQUIRED_GROUP).
 *
 * Copilot review fix on PR #94: docstring previously claimed "space/
 * comma-separated value" support; the implementation never did that.
 */
function extractGroupsClaim(raw: unknown): string[] | undefined {
  if (Array.isArray(raw)) {
    const out = raw.filter((v): v is string => typeof v === "string");
    return out.length > 0 ? out : undefined;
  }
  if (typeof raw === "string" && raw.length > 0) return [raw];
  return undefined;
}

function loginError(url: URL, code: string): NextResponse {
  const target = publicLoginUrl(url);
  target.searchParams.set("error", code);
  return NextResponse.redirect(target);
}
