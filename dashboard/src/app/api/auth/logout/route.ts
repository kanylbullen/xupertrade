import { createHash } from "node:crypto";

import { eq } from "drizzle-orm";
import { NextResponse } from "next/server";

import {
  SESSION_COOKIE,
  getSessionSecret,
  verifySession,
} from "@/lib/auth";
import { clearKey } from "@/lib/crypto/k-cache";
import { db, tenants } from "@/lib/db";
import { markSessionRevoked } from "@/lib/session-store";

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

/**
 * H-3 defence-in-depth on logout. Three layers, each independent so a
 * single-layer failure doesn't leave the user partially logged out:
 *
 *   1. Server-side revocation list (`session:revoked:<sha256(cookie)>`)
 *      so a stolen cookie copy stops working immediately.
 *   2. K-cache eviction so a stolen cookie can't resolve the tenant's
 *      cached decryption key from `dashboard:k-cache:<tenant>:<sid>`.
 *   3. Cookie clear on the response so the browser drops it.
 *
 * Logout MUST NOT error — every branch is wrapped or guarded so a
 * Redis hiccup, missing tenant row, or invalid cookie still gives
 * the user back a clean response with the cookie cleared.
 */
async function performLogout(req: Request): Promise<NextResponse> {
  const cookieHeader = req.headers.get("cookie") ?? "";
  const match = cookieHeader.match(
    new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`),
  );
  const cookieValue = match?.[1] ?? "";

  if (cookieValue) {
    // Layer 1: revocation list. Best-effort; never propagate errors.
    try {
      await markSessionRevoked(cookieValue);
    } catch (err) {
      console.warn("[logout] markSessionRevoked failed:", err);
    }

    // Layer 2: evict K-cache for this (tenant, session). Requires
    // resolving the tenant from the cookie's `sub`. Both lookups are
    // best-effort — a missing/invalid cookie or absent tenant row
    // just means there's nothing to evict.
    try {
      const sessionId = createHash("sha256")
        .update(cookieValue)
        .digest("hex")
        .slice(0, 32);
      const secret = await getSessionSecret().catch(() => "");
      const payload = secret ? verifySession(cookieValue, secret) : null;
      if (payload) {
        const rows = await db
          .select({ id: tenants.id })
          .from(tenants)
          .where(eq(tenants.authentikSub, payload.sub))
          .limit(1);
        if (rows.length > 0) {
          await clearKey(rows[0].id, sessionId);
        }
      }
    } catch (err) {
      console.warn("[logout] k-cache eviction failed:", err);
    }
  }

  return clearSessionCookie(NextResponse.json({ ok: true }));
}

export async function POST(req: Request) {
  return performLogout(req);
}

// Fallback for users who navigate to this URL directly (e.g. typed in
// browser bar or followed an old link). Still revokes server-side
// before redirecting.
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

  // Reuse the POST path's revocation + cache-eviction logic, then
  // swap the body for a redirect on the same response cookies.
  await performLogout(req).catch((err) => {
    console.warn("[logout GET] performLogout failed:", err);
  });
  return clearSessionCookie(NextResponse.redirect(target));
}
