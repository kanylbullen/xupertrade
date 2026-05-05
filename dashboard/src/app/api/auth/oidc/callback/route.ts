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

  // Sign the session cookie. Secret comes from the API_KEY-gated bot
  // endpoint so it's never publicly exposed.
  const secret = await getSessionSecret(true);
  if (!secret) {
    return loginError(url, "session-secret-unavailable");
  }
  const sessionValue = signSession(newSessionPayload(sub), secret);

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

function loginError(url: URL, code: string): NextResponse {
  const target = publicLoginUrl(url);
  target.searchParams.set("error", code);
  return NextResponse.redirect(target);
}
