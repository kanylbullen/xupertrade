import { hash as bcryptHash } from "@node-rs/bcrypt";

import { invalidateAuthCache } from "@/lib/auth";
import {
  setAuthConfig,
  type AuthMode,
} from "@/lib/auth-config";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

// String fields that pass through verbatim (mode is special-cased
// for enum validation below).
const PASSTHROUGH_KEYS = [
  "basic_user",
  "oidc_issuer",
  "oidc_client_id",
  "oidc_client_secret",
  "oidc_scopes",
] as const;

type PassthroughKey = (typeof PASSTHROUGH_KEYS)[number];

function isValidMode(v: unknown): v is AuthMode {
  return v === "disabled" || v === "basic" || v === "oidc";
}

export async function POST(req: Request) {
  // Operator-only: auth mode + OIDC config are host-level concerns
  // (single Authentik provider serving all tenants). Without this
  // gate any signed-in tenant could repoint OIDC to their own
  // Authentik instance and intercept future logins.
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const raw = await req.json().catch(() => null);
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return Response.json({ error: "body must be a JSON object" }, { status: 400 });
  }
  const body = raw as Record<string, unknown>;

  const updates: {
    mode?: AuthMode;
    basic_user?: string;
    basic_hash?: string;
    oidc_issuer?: string;
    oidc_client_id?: string;
    oidc_client_secret?: string;
    oidc_scopes?: string;
  } = {};

  if ("mode" in body) {
    const m = body.mode;
    if (typeof m !== "string") {
      return Response.json({ error: "mode must be a string" }, { status: 400 });
    }
    if (m !== "" && !isValidMode(m)) {
      return Response.json(
        { error: "mode must be 'disabled' | 'basic' | 'oidc'" },
        { status: 400 },
      );
    }
    if (isValidMode(m)) updates.mode = m;
  }

  for (const key of PASSTHROUGH_KEYS) {
    if (!(key in body)) continue;
    const val = body[key];
    if (typeof val !== "string") {
      return Response.json(
        { error: `${key} must be a string` },
        { status: 400 },
      );
    }
    (updates as Record<PassthroughKey, string>)[key] = val;
  }

  // basic_password (plaintext) → basic_hash (bcrypt). The bot's
  // auth_configure did the same hashing step; we do it here now
  // so PR 4c can delete the bot endpoint. Plaintext leaves the
  // process immediately after hashing — never persisted.
  if ("basic_password" in body) {
    const pw = body.basic_password;
    if (typeof pw === "string" && pw.length > 0) {
      // Cost 12 matches Python's bcrypt.gensalt() default. Sync
      // because @node-rs/bcrypt does work off-thread internally.
      updates.basic_hash = await bcryptHash(pw, 12);
    }
  }

  await setAuthConfig(updates);
  // Bust the in-process cache so the next page load sees the new mode
  invalidateAuthCache();
  return Response.json({ ok: true });
}
