/**
 * /api/tenant/me/bots — list and create.
 *
 * Multi-tenancy Phase 3a.
 *
 *   GET   → list this tenant's bot DB rows (no live docker status
 *           in v1 — use GET /api/tenant/me/bots/[id] for that)
 *   POST  → create a new bot for this tenant + start its container
 *           body: { mode: 'paper' | 'testnet' | 'mainnet' }
 *           requires unlock (we decrypt the tenant's secrets and
 *           inject them as env vars)
 *
 * All responses use camelCase (matches Drizzle's row shape and
 * existing dashboard endpoints). Snake_case in API contracts is
 * intentional only for env-var-shaped fields (e.g. secret keys).
 */

import { randomUUID } from "node:crypto";

import { and, eq, sql } from "drizzle-orm";

import { db, tenantBots, tenantSecrets } from "@/lib/db";
import {
  type BotMode,
  containerName,
  isValidMode,
  requiredSecretsForMode,
} from "@/lib/bot-orchestrator";
import { requireTenant } from "@/lib/tenant";

import { decryptAndStart } from "./_decrypt-and-start";

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

  // Reserve the DB row FIRST so a concurrent POST can't claim the
  // same (tenant, mode) slot. UNIQUE(tenant_id, mode) enforces
  // uniqueness at the DB layer — if we lose the race we map ONLY
  // the postgres unique-violation (23505) to 409.
  const botId = randomUUID();
  // Derive the container-name stub without going through buildSpec —
  // we don't have an API key yet (decryptAndStart generates one) and
  // buildSpec now requires it.
  const containerNameStub = containerName(tenant.id, mode as BotMode);
  try {
    await db.insert(tenantBots).values({
      id: botId,
      tenantId: tenant.id,
      mode,
      containerName: containerNameStub,
    });
  } catch (err) {
    if (isUniqueViolation(err)) {
      return Response.json(
        { error: `bot for mode=${mode} already exists for this tenant` },
        { status: 409 },
      );
    }
    throw err;
  }

  // Slot is ours. Decrypt + start. On any failure inside, roll back
  // the row reservation so the next POST can retry cleanly.
  const result = await decryptAndStart({
    req,
    tenant,
    botId,
    mode: mode as BotMode,
  });
  if (result.kind === "response") {
    await db
      .delete(tenantBots)
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      )
      .catch(() => undefined);
    return result.response;
  }
  return Response.json({ bot: result.bot });
}

/**
 * Postgres unique-constraint violation. postgres-js wraps errors
 * with a `code` field; 23505 is the SQLSTATE for unique_violation.
 */
function isUniqueViolation(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "code" in err &&
    (err as { code: string }).code === "23505"
  );
}
