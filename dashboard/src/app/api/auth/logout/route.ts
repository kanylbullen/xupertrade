import { NextResponse } from "next/server";
import { SESSION_COOKIE } from "@/lib/auth";

export const dynamic = "force-dynamic";

function clearSessionCookie(res: NextResponse): NextResponse {
  res.cookies.set(SESSION_COOKIE, "", {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return res;
}

export async function POST() {
  return clearSessionCookie(NextResponse.json({ ok: true }));
}

// Fallback for users who navigate to this URL directly (e.g. typed in
// browser bar or followed an old link). Clears the cookie and redirects.
export async function GET(req: Request) {
  // Redirect to /login on the public hostname (PUBLIC_URL), not the
  // docker container hostname embedded in req.url.
  const publicBase =
    (process.env.PUBLIC_URL || process.env.DASHBOARD_URL || "").trim().replace(/\/+$/, "");
  const target = publicBase
    ? new URL("/login", publicBase + "/")
    : (() => {
        const u = new URL(req.url);
        u.pathname = "/login";
        u.search = "";
        return u;
      })();
  return clearSessionCookie(NextResponse.redirect(target));
}
