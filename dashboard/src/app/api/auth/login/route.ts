import { NextResponse } from "next/server";
import {
  fetchAuthConfig,
  getSessionSecret,
  signSession,
  newSessionPayload,
  COOKIE_OPTIONS,
} from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as {
    username?: string;
    password?: string;
  };
  const username = (body.username ?? "").trim();
  const password = body.password ?? "";

  if (!username || !password) {
    return NextResponse.json({ ok: false, error: "missing-credentials" }, { status: 400 });
  }

  const cfg = await fetchAuthConfig(true);
  if (cfg === null) {
    // Bot unreachable — can't verify auth state. Fail closed rather
    // than silently allowing login.
    return NextResponse.json(
      { ok: false, error: "bot-unreachable" },
      { status: 503 },
    );
  }
  // Allow basic auth as a fallback even when mode=oidc, as long as a
  // basic user is configured. This is the path the /login fallback link
  // uses when OIDC misbehaves.
  if (cfg.mode === "disabled" || !cfg.basic_user_set) {
    return NextResponse.json(
      { ok: false, error: "basic-auth-not-enabled" },
      { status: 400 },
    );
  }

  // Proxy verification to the bot
  const botUrl = process.env.BOT_API_URL_TESTNET || "http://bot-testnet:8001";
  let verify;
  try {
    const res = await fetch(`${botUrl}/api/auth/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    verify = (await res.json()) as { ok?: boolean };
  } catch {
    return NextResponse.json({ ok: false, error: "bot-unreachable" }, { status: 502 });
  }

  if (!verify.ok) {
    return NextResponse.json({ ok: false, error: "invalid-credentials" }, { status: 401 });
  }

  const secret = await getSessionSecret(true);
  if (!secret) {
    // No session secret available — bot is unreachable, or the dashboard
    // doesn't have API_KEY set so it can't authenticate to the bot's
    // gated /api/auth/session-secret endpoint. Either way the user can't
    // actually log in until the operator fixes the env. Reuse the
    // `bot-unreachable` error code since the login UI already maps it.
    return NextResponse.json(
      { ok: false, error: "bot-unreachable" },
      { status: 503 },
    );
  }
  const payload = newSessionPayload(username);
  const cookieValue = signSession(payload, secret);
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE_OPTIONS.name, cookieValue, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: COOKIE_OPTIONS.maxAge,
  });
  return res;
}
