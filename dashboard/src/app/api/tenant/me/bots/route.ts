/**
 * /api/tenant/me/bots — list and create.
 *
 * Multi-tenancy Phase 3a.
 *
 *   GET   → list this tenant's bots (DB rows, with current docker
 *           status if a container_id is set — best-effort)
 *   POST  → create a new bot for this tenant + start its container
 *           body: { mode: 'paper' | 'testnet' | 'mainnet' }
 *           requires unlock (we decrypt the tenant's secrets and
 *           inject them as env vars)
 */

import { randomUUID } from "node:crypto";

import { and, eq, sql } from "drizzle-orm";

import { decryptSecret } from "@/lib/crypto/secrets";
import { db, tenantBots, tenantSecrets } from "@/lib/db";
import {
  type BotMode,
  buildSpec,
  isValidMode,
  requiredSecretsForMode,
  startBot,
} from "@/lib/bot-orchestrator";
import { requireTenant, requireUnlockedKey } from "@/lib/tenant";

export const dynamic = "force-dynamic";

const MAX_BOTS_PER_TENANT = 3;  // one per mode

export async function GET(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const rows = await db
    .select()
    .from(tenantBots)
    .where(eq(tenantBots.tenantId, tenant.id))
    .orderBy(tenantBots.mode);

  return Response.json({ bots: rows });
}

export async function POST(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json({ error: "body must be valid JSON" }, { status: 400 });
  }
  const mode = (body as { mode?: unknown }).mode;
  if (!isValidMode(mode)) {
    return Response.json(
      { error: "field 'mode' must be 'paper' | 'testnet' | 'mainnet'" },
      { status: 400 },
    );
  }

  // Multi-bot gate: single-bot tenants can have at most 1 bot total;
  // multi-bot tenants up to 3 (one per mode). The DB-level UNIQUE
  // (tenant_id, mode) takes care of the per-mode uniqueness.
  const existing = await db
    .select({ count: sql<number>`count(*)::int` })
    .from(tenantBots)
    .where(eq(tenantBots.tenantId, tenant.id));
  const existingCount = existing[0]?.count ?? 0;

  if (!tenant.multiBotEnabled && existingCount >= 1) {
    return Response.json(
      {
        error:
          "single-bot tenant already has a bot — stop existing first or contact operator to enable multi-bot mode",
      },
      { status: 409 },
    );
  }
  if (existingCount >= MAX_BOTS_PER_TENANT) {
    return Response.json(
      { error: `maximum ${MAX_BOTS_PER_TENANT} bots per tenant (one per mode)` },
      { status: 409 },
    );
  }

  // Validate required secrets are present BEFORE we unlock — cheap check.
  const required = requiredSecretsForMode(mode as BotMode);
  if (required.length > 0) {
    const presentRows = await db
      .select({ key: tenantSecrets.key })
      .from(tenantSecrets)
      .where(eq(tenantSecrets.tenantId, tenant.id));
    const present = new Set(presentRows.map((r) => r.key));
    const missing = required.filter((k) => !present.has(k));
    if (missing.length > 0) {
      return Response.json(
        {
          error: `missing required secrets for mode=${mode}: ${missing.join(", ")}`,
        },
        { status: 422 },
      );
    }
  }

  // Now unlock K and decrypt all secrets the bot might need.
  let k: Buffer;
  try {
    k = await requireUnlockedKey(req, tenant);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }
  const secretRows = await db
    .select()
    .from(tenantSecrets)
    .where(eq(tenantSecrets.tenantId, tenant.id));
  const decryptedSecrets: Record<string, string> = {};
  for (const row of secretRows) {
    try {
      decryptedSecrets[row.key] = decryptSecret(k, row.ciphertext, row.nonce);
    } catch {
      // GCM auth tag mismatch — wrong K (passphrase changed?) or
      // tampered ciphertext. Refuse to start with partial secrets.
      return Response.json(
        {
          error: `failed to decrypt secret '${row.key}' — passphrase may have changed; re-unlock`,
        },
        { status: 500 },
      );
    }
  }

  // Reserve the DB row first so a concurrent POST can't claim the
  // same (tenant, mode) slot. UNIQUE(tenant_id, mode) enforces it
  // at the DB layer — if we lose the race the INSERT throws and we
  // map to 409.
  const botId = randomUUID();
  const spec = buildSpec({
    botId,
    tenantId: tenant.id,
    mode: mode as BotMode,
    decryptedSecrets,
  });
  try {
    await db.insert(tenantBots).values({
      id: botId,
      tenantId: tenant.id,
      mode,
      containerName: spec.name,
    });
  } catch {
    return Response.json(
      { error: `bot for mode=${mode} already exists for this tenant` },
      { status: 409 },
    );
  }

  // Now start the container.
  try {
    const info = await startBot({
      botId,
      tenantId: tenant.id,
      mode: mode as BotMode,
      decryptedSecrets,
    });
    await db
      .update(tenantBots)
      .set({
        containerId: info.id,
        isRunning: true,
        lastStartedAt: sql`now()`,
      })
      .where(eq(tenantBots.id, botId));
    return Response.json({
      bot: {
        id: botId,
        tenant_id: tenant.id,
        mode,
        container_id: info.id,
        container_name: info.name,
        is_running: true,
      },
    });
  } catch (err) {
    // Container failed to start — roll back the DB row so the next
    // POST can try again cleanly.
    await db
      .delete(tenantBots)
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      );
    const message = err instanceof Error ? err.message : "unknown docker error";
    return Response.json(
      { error: `failed to start container: ${message}` },
      { status: 500 },
    );
  }
}
