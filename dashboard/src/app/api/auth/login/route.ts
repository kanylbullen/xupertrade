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
import { getClientIp } from "@/lib/client-ip";
import { checkRateLimit } from "@/lib/rate-limit";

export const dynamic = "force-dynamic";

// H-2: precomputed bcrypt hash used as the verify target on
// user-not-found so the bcrypt CPU cost is paid regardless —
// eliminates the ~250ms timing oracle that would otherwise let an
// attacker enumerate the configured `basic_user`.
//
// HARDCODED rather than `hashSync(...)` at module load (Copilot review
// fix on PR #92) so we don't pay an unnecessary ~250ms cold-start
// cost on every Next.js process spawn. Any valid cost-12 bcrypt hash
// works; this one was generated once locally with cost 12 and the
// plaintext "dummy-not-a-real-password". The plaintext is public —
// there is nothing to protect here, the hash exists purely to make
// `bcryptVerify` do work.
const DUMMY_HASH =
  "$2y$12$ZkgAhco9SGGGbpEEVfxrgOb6BPW73tCuEMAbrPaC4QunY/iOBqDaa";

const RATE_LIMIT_MAX = 10;
const RATE_LIMIT_WINDOW_SEC = 900; // 15 minutes

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
    // Malformed stored hash. Pay the bcrypt cost on the dummy hash so
    // a corrupted hash in Redis doesn't reintroduce a timing oracle
    // (Copilot review fix on PR #92). Auth still fails.
    try {
      await bcryptVerify(password, DUMMY_HASH);
    } catch {
      // Even the dummy hash is unreachable — auth simply fails.
    }
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
