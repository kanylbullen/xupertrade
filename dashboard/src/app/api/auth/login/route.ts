import { NextResponse } from "next/server";
import { verify as bcryptVerify } from "@node-rs/bcrypt";

import {
  fetchAuthConfig,
  getSessionSecret,
  signSession,
  newSessionPayload,
  COOKIE_OPTIONS,
} from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";

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
    // Redis unreachable — can't verify auth state. Fail closed
    // rather than silently allowing login.
    return NextResponse.json(
      { ok: false, error: "auth-state-unavailable" },
      { status: 503 },
    );
  }
  // Allow basic auth as a fallback even when mode=oidc, as long as a
  // basic user is configured. This is the path the /login fallback
  // link uses when OIDC misbehaves.
  if (cfg.mode === "disabled" || !cfg.basic_user_set) {
    return NextResponse.json(
      { ok: false, error: "basic-auth-not-enabled" },
      { status: 400 },
    );
  }

  // PR 4a: bcrypt-compare directly against the stored hash in
  // Redis. Replaces the proxy to bot's /api/auth/verify which
  // did the same operation server-side. Constant-time bcrypt
  // check; same security properties.
  const raw = await getAuthConfig();
  const storedUser = raw.basic_user;
  const storedHash = raw.basic_hash;
  if (!storedUser || !storedHash) {
    return NextResponse.json(
      { ok: false, error: "basic-auth-not-enabled" },
      { status: 400 },
    );
  }
  if (username !== storedUser) {
    return NextResponse.json(
      { ok: false, error: "invalid-credentials" },
      { status: 401 },
    );
  }
  let passwordOk = false;
  try {
    passwordOk = await bcryptVerify(password, storedHash);
  } catch {
    // Malformed hash in Redis — treat as failed auth, never crash.
    return NextResponse.json(
      { ok: false, error: "invalid-credentials" },
      { status: 401 },
    );
  }
  if (!passwordOk) {
    return NextResponse.json(
      { ok: false, error: "invalid-credentials" },
      { status: 401 },
    );
  }

  const secret = await getSessionSecret(true);
  if (!secret) {
    // No session secret available — Redis unreachable. The
    // operator must fix it before logins can resume.
    return NextResponse.json(
      { ok: false, error: "auth-state-unavailable" },
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
