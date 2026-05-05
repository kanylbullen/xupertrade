import { NextResponse, type NextRequest } from "next/server";
import {
  fetchAuthConfig,
  getSessionSecret,
  verifySession,
  SESSION_COOKIE,
} from "@/lib/auth";

// Public paths — never require auth
const PUBLIC_PATHS = new Set([
  "/login",
  "/api/auth/login",
  "/api/auth/logout",
  "/api/auth/config",
  "/api/auth/oidc/start",
  "/api/auth/oidc/callback",
]);

function isPublic(pathname: string): boolean {
  if (PUBLIC_PATHS.has(pathname)) return true;
  if (pathname.startsWith("/_next/")) return true;
  if (pathname.startsWith("/favicon")) return true;
  if (pathname.startsWith("/static/")) return true;
  return false;
}

export async function proxy(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (isPublic(pathname)) return NextResponse.next();

  const cfg = await fetchAuthConfig();
  if (cfg.mode === "disabled") return NextResponse.next();

  const cookie = req.cookies.get(SESSION_COOKIE)?.value;
  const secret = await getSessionSecret();
  const session = cookie && secret ? verifySession(cookie, secret) : null;
  if (session) return NextResponse.next();

  // Build the login redirect URL on the public hostname (PUBLIC_URL),
  // not the docker container hostname embedded in req.nextUrl. Falls
  // back to req.nextUrl if PUBLIC_URL is unset.
  const publicBase =
    (process.env.PUBLIC_URL || process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, "");
  const loginUrl = publicBase
    ? new URL("/login", publicBase + "/")
    : req.nextUrl.clone();
  if (!publicBase) {
    loginUrl.pathname = "/login";
    loginUrl.search = "";
  }
  loginUrl.searchParams.set("next", pathname + req.nextUrl.search);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
