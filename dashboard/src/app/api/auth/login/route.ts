import { NextResponse } from "next/server";
import { hashSync, verify as bcryptVerify } from "@node-rs/bcrypt";

import {
  fetchAuthConfig,
  getSessionSecret,
  signSession,
  newSessionPayload,
  COOKIE_OPTIONS,
} from "@/lib/auth";
import { getAuthConfig } from "@/lib/auth-config";
import { checkRateLimit } from "@/lib/rate-limit";

export const dynamic = "force-dynamic";

// H-2: pre-compute a dummy bcrypt hash once at module load. Used as
// the verify target on user-not-found so the bcrypt CPU cost is paid
// regardless — eliminates the ~250ms timing oracle that would otherwise
// let an attacker enumerate the configured `basic_user`.
//
// The plaintext is a public sentinel — there is nothing to protect
// here; the hash exists purely to make `bcryptVerify` do work. Cost
// 12 matches the cost typically used for stored basic-auth hashes
// elsewhere in the dashboard so the timing profile lines up.
const DUMMY_HASH = hashSync("dummy-not-a-real-password", 12);

const RATE_LIMIT_MAX = 10;
const RATE_LIMIT_WINDOW_SEC = 900; // 15 minutes

function getClientIp(req: Request): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const first = xff.split(",")[0]?.trim();
    if (first) return first;
  }
  return "unknown";
}

function rateLimited(retryAfterSeconds: number): NextResponse {
  return NextResponse.json(
    { ok: false, error: "rate-limited", retry_after_seconds: retryAfterSeconds },
    {
      status: 429,
      headers: { "Retry-After": String(retryAfterSeconds) },
    },
  );
}

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

  // H-2: rate-limit per-IP and per-username before any expensive work
  // (auth-config fetch, bcrypt verify). Two buckets so a flood from a
  // single IP doesn't lock out a separate user, and a flood guessing
  // one username doesn't lock out other usernames from the same IP.
  const ip = getClientIp(req);
  const ipRl = await checkRateLimit(
    "auth-login-ip",
    ip,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW_SEC,
  );
  if (!ipRl.allowed) {
    return rateLimited(ipRl.resetInSeconds);
  }
  const userRl = await checkRateLimit(
    "auth-login-user",
    username.toLowerCase(),
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW_SEC,
  );
  if (!userRl.allowed) {
    return rateLimited(userRl.resetInSeconds);
  }

  const cfg = await fetchAuthConfig(true);
  if (cfg === null) {
    // Redis unreachable — can't verify auth state. Fail closed
    // rather than silently allowing login.
    return NextResponse.json(
      { ok: false, error: "bot-unreachable" },
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

  // H-2: avoid the user-existence timing oracle. Always run bcrypt —
  // against the stored hash if usernames match, otherwise against the
  // pre-computed dummy hash so the CPU cost is identical. Combine
  // both checks into a single 401 path so an attacker can't tell
  // "user exists, wrong password" from "user does not exist".
  const userMatches = username === storedUser;
  const hashToCheck = userMatches ? storedHash : DUMMY_HASH;
  let bcryptOk = false;
  try {
    bcryptOk = await bcryptVerify(password, hashToCheck);
  } catch {
    // Malformed hash in Redis — treat as failed auth, never crash.
    bcryptOk = false;
  }
  if (!userMatches || !bcryptOk) {
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
