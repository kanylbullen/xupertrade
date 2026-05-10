/**
 * POST /api/tenant/me/passphrase — set the tenant's initial passphrase.
 *
 * Multi-tenancy Phase 2c. v1 only handles the initial-set case
 * (no `oldPassphrase`); change-passphrase + re-encrypt-all-secrets
 * comes in a later phase (it touches every row in tenant_secrets so
 * needs its own design + tests).
 *
 * Side effects:
 *   - generates a random Argon2id salt
 *   - derives K via Argon2id(passphrase, salt)
 *   - stores salt + verifier (HMAC of K) on the tenant row
 *   - K is NOT cached here; user calls /unlock to do that
 */

import {
  KEY_BYTES,
  deriveKey,
  generateSalt,
  makeVerifier,
} from "@/lib/crypto/passphrase";
import { db, tenants } from "@/lib/db";
import { requireTenant } from "@/lib/tenant";
import { eq } from "drizzle-orm";

export const dynamic = "force-dynamic";

const MIN_PASSPHRASE_LEN = 12;

export async function POST(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (resp) {
    return resp as Response;
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: "body must be valid JSON" }, { status: 400 });
  }
  if (typeof body !== "object" || body === null) {
    return Response.json({ error: "body must be a JSON object" }, { status: 400 });
  }
  const passphrase = (body as { passphrase?: unknown }).passphrase;
  if (typeof passphrase !== "string") {
    return Response.json(
      { error: "field 'passphrase' is required (string)" },
      { status: 400 },
    );
  }
  if (passphrase.length < MIN_PASSPHRASE_LEN) {
    return Response.json(
      { error: `passphrase must be at least ${MIN_PASSPHRASE_LEN} characters` },
      { status: 400 },
    );
  }

  // v1: refuse if a passphrase already exists. Change-flow comes later.
  if (tenant.passphraseVerifier !== null) {
    return Response.json(
      {
        error:
          "passphrase already set; change-passphrase flow not yet supported",
      },
      { status: 409 },
    );
  }

  const salt = generateSalt();
  const k = await deriveKey(passphrase, salt);
  if (k.length !== KEY_BYTES) {
    // Defensive — deriveKey is supposed to enforce this.
    return Response.json({ error: "internal kdf error" }, { status: 500 });
  }
  const verifier = makeVerifier(k);

  await db
    .update(tenants)
    .set({
      passphraseSalt: salt,
      passphraseVerifier: verifier,
    })
    .where(eq(tenants.id, tenant.id));

  return Response.json({ passphrase_set: true });
}
