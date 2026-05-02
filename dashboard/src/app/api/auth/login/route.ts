import { NextResponse } from "next/server";
import {
  fetchAuthConfig,
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

  const payload = newSessionPayload(username);
  const cookieValue = signSession(payload, cfg.session_secret);
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE_OPTIONS.name, cookieValue, {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: COOKIE_OPTIONS.maxAge,
  });
  return res;
}
