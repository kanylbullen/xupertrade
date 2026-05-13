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

import { and, eq, sql } from "drizzle-orm";

import { encryptSecret } from "@/lib/crypto/secrets";
import { db, tenantSecrets } from "@/lib/db";
import { requireTenant, requireUnlockedKey } from "@/lib/tenant";

export const dynamic = "force-dynamic";

const KEY_PATTERN = /^[A-Z0-9_]{1,64}$/;
const MAX_VALUE_BYTES = 4096; // 4KB plenty for HL keys, tokens, addresses

/**
 * Allowlist of env-var names a tenant is permitted to set via PUT.
 *
 * Security audit C-1 (2026-05-12): without an allowlist, a tenant
 * could PUT arbitrary `[A-Z0-9_]{1,64}` keys and have them injected
 * into their bot container's env. The orchestrator's `systemEnv`
 * only enumerated 8 keys, so a tenant could clobber operator
 * policy env vars (mainnet allowlist, exposure cap, fee rate,
 * trade-rate alarm, HL timeouts, etc.).
 *
 * Defence in depth: getOrchestratorSystemEnv() now explicitly sets
 * those policy vars too (systemEnv wins on collision in buildSpec),
 * but we still gate at the write boundary so the DB never holds
 * tenant-set values for policy keys.
 *
 * DELETE is intentionally not gated by this set — tenants who wrote
 * non-allowlisted keys before this fix landed must be able to clean
 * them up without an operator-side migration.
 */
const TENANT_ALLOWED_SECRETS = new Set([
  "HYPERLIQUID_PRIVATE_KEY",
  "HYPERLIQUID_ACCOUNT_ADDRESS",
  // Optional separate mainnet wallet — the credentials UI
  // (settings/credentials) writes these regardless of whether the
  // orchestrator currently injects them. TODO(orchestrator): pick
  // MAINNET_* over the testnet keys when EXCHANGE_MODE=mainnet.
  "HYPERLIQUID_MAINNET_PRIVATE_KEY",
  "HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS",
  "TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID",
  "VAULT_TRACKING_ADDRESS",
]);

function validKey(key: unknown): key is string {
  return typeof key === "string" && KEY_PATTERN.test(key);
}

function isAllowedForPut(key: string): boolean {
  return TENANT_ALLOWED_SECRETS.has(key);
}

const EXPIRY_TRACKED_KEYS = new Set([
  "HYPERLIQUID_PRIVATE_KEY",
  "HYPERLIQUID_MAINNET_PRIVATE_KEY",
]);

// Accept either `YYYY-MM-DD` (the native <input type=date> wire format)
// or a full ISO-8601 timestamp. Returns null for empty input; throws
// Response on invalid input so the PUT handler can return 400.
function parseExpiresAt(raw: unknown): Date | null {
  if (raw === null || raw === undefined || raw === "") return null;
  if (typeof raw !== "string") {
    throw Response.json(
      { error: "expiresAt must be a string (YYYY-MM-DD) or null" },
      { status: 400 },
    );
  }
  // Bare date → interpret as UTC midnight (consistent with the input
  // type's wire format being a calendar date, not an instant).
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw);
  const parsed = new Date(dateOnly ? `${raw}T00:00:00Z` : raw);
  if (Number.isNaN(parsed.getTime())) {
    throw Response.json(
      { error: "expiresAt is not a valid date" },
      { status: 400 },
    );
  }
  return parsed;
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
  if (!isAllowedForPut(key)) {
    return Response.json(
      {
        error:
          `key '${key}' is not a tenant-settable secret. ` +
          `Allowed: ${[...TENANT_ALLOWED_SECRETS].sort().join(", ")}`,
      },
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

  // expiresAt is only meaningful for the two HL private-key rows.
  // For other keys we silently drop any client-supplied value so the
  // UI can send the field unconditionally without us 400-ing.
  let expiresAt: Date | null = null;
  if (EXPIRY_TRACKED_KEYS.has(key)) {
    const rawExp = (body as { expiresAt?: unknown })?.expiresAt;
    try {
      expiresAt = parseExpiresAt(rawExp);
    } catch (err) {
      if (err instanceof Response) return err;
      throw err;
    }
  }

  let k: Buffer;
  try {
    k = await requireUnlockedKey(req, tenant);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const { ciphertext, nonce } = encryptSecret(k, value);

  const insertValues = {
    tenantId: tenant.id,
    key,
    ciphertext,
    nonce,
    ...(EXPIRY_TRACKED_KEYS.has(key) ? { expiresAt } : {}),
  };
  const updateSet: Record<string, unknown> = {
    ciphertext,
    nonce,
    // Use DB-side `NOW()` so the update path matches the insert
    // path's `defaultNow()` source. Avoids ordering surprises if
    // app and DB clocks differ.
    updatedAt: sql`now()`,
  };
  if (EXPIRY_TRACKED_KEYS.has(key)) {
    updateSet.expiresAt = expiresAt;
  }
  await db
    .insert(tenantSecrets)
    .values(insertValues)
    .onConflictDoUpdate({
      target: [tenantSecrets.tenantId, tenantSecrets.key],
      set: updateSet,
    });

  return Response.json({
    key,
    set: true,
    ...(EXPIRY_TRACKED_KEYS.has(key)
      ? { expiresAt: expiresAt ? expiresAt.toISOString() : null }
      : {}),
  });
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
