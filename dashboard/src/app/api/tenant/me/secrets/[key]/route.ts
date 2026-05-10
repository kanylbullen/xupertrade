/**
 * Per-key secret CRUD — multi-tenancy Phase 2d.
 *
 *   PUT    /api/tenant/me/secrets/[key]    body: { value: string }
 *     → encrypts under cached K, upserts into tenant_secrets
 *     → 401 if tenant locked (POST /unlock first)
 *
 *   DELETE /api/tenant/me/secrets/[key]
 *     → removes the row; idempotent (404 if not found)
 *     → does NOT require unlock (delete is metadata, not encrypt op)
 *
 * The set of valid `key` values is intentionally restricted (alphanum
 * + underscore, 1-64 chars) to prevent operators from misusing the
 * endpoint as a generic kv store and to keep DB row sizes bounded.
 */

import { and, eq } from "drizzle-orm";

import { encryptSecret } from "@/lib/crypto/secrets";
import { db, tenantSecrets } from "@/lib/db";
import { requireTenant, requireUnlockedKey } from "@/lib/tenant";

export const dynamic = "force-dynamic";

const KEY_PATTERN = /^[A-Z0-9_]{1,64}$/;
const MAX_VALUE_BYTES = 4096; // 4KB plenty for HL keys, tokens, addresses

function validKey(key: unknown): key is string {
  return typeof key === "string" && KEY_PATTERN.test(key);
}

type Params = { params: Promise<{ key: string }> };

export async function PUT(req: Request, ctx: Params): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { key } = await ctx.params;
  if (!validKey(key)) {
    return Response.json(
      { error: "key must match [A-Z0-9_]{1,64}" },
      { status: 400 },
    );
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: "body must be valid JSON" }, { status: 400 });
  }
  const value = (body as { value?: unknown })?.value;
  if (typeof value !== "string") {
    return Response.json(
      { error: "field 'value' is required (string)" },
      { status: 400 },
    );
  }
  if (Buffer.byteLength(value, "utf8") > MAX_VALUE_BYTES) {
    return Response.json(
      { error: `value too large (max ${MAX_VALUE_BYTES} bytes)` },
      { status: 413 },
    );
  }

  let k: Buffer;
  try {
    k = await requireUnlockedKey(req, tenant);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { ciphertext, nonce } = encryptSecret(k, value);

  await db
    .insert(tenantSecrets)
    .values({
      tenantId: tenant.id,
      key,
      ciphertext,
      nonce,
    })
    .onConflictDoUpdate({
      target: [tenantSecrets.tenantId, tenantSecrets.key],
      set: {
        ciphertext,
        nonce,
        updatedAt: new Date(),
      },
    });

  return Response.json({ key, set: true });
}

export async function DELETE(req: Request, ctx: Params): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { key } = await ctx.params;
  if (!validKey(key)) {
    return Response.json(
      { error: "key must match [A-Z0-9_]{1,64}" },
      { status: 400 },
    );
  }

  const deleted = await db
    .delete(tenantSecrets)
    .where(
      and(
        eq(tenantSecrets.tenantId, tenant.id),
        eq(tenantSecrets.key, key),
      ),
    )
    .returning({ key: tenantSecrets.key });

  if (deleted.length === 0) {
    return Response.json({ error: "secret not found" }, { status: 404 });
  }
  return Response.json({ key, deleted: true });
}
