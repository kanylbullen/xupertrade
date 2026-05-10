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
import {
  generateRolePassword,
  provisionRole,
  tenantDatabaseUrl,
} from "@/lib/tenant-pg-role";

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
      // GCM auth tag mismatch — wrong K (passphrase changed under us?)
      // or tampered ciphertext. Treat as 401: the cached K is no longer
      // valid for this tenant, the user must re-unlock with the
      // current passphrase. This is client-recoverable, not a server
      // error.
      return Response.json(
        {
          error: `failed to decrypt secret '${row.key}' — re-unlock with current passphrase`,
        },
        { status: 401 },
      );
    }
  }

  // Reserve the DB row FIRST so a concurrent POST can't claim the
  // same (tenant, mode) slot — and so a duplicate request doesn't
  // rotate the shared role password before failing on UNIQUE
  // (PR #46 review fix). UNIQUE(tenant_id, mode) enforces uniqueness
  // at the DB layer — if we lose the race we map ONLY the postgres
  // unique-violation (23505) to 409. Other errors (connectivity,
  // schema drift) propagate as 500 so we don't mis-attribute them.
  const botId = randomUUID();
  const containerNameStub = buildSpec({
    botId,
    tenantId: tenant.id,
    mode: mode as BotMode,
    decryptedSecrets: {},
  }).name;
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

  // Slot is ours. NOW provision the tenant's Postgres role and
  // build the connection string. provisionRole is idempotent
  // (re-runs ALTER ROLE with a fresh password). Bot connects as
  // this role; alembic 0010's RLS policy filters every query so
  // cross-tenant rows are invisible at the DB layer.
  //
  // v1 caveat (per tenant-pg-role.ts): rotating the password on
  // every bot-create means concurrent bots of the same tenant share
  // the role but only the newest container has the right password.
  // Acceptable for single-bot tenants (closed-beta); multi-bot
  // password sync gets a proper fix before multi_bot_enabled is
  // exposed beyond the operator.
  const tenantPassword = generateRolePassword();
  let tenantDbUrl: string;
  try {
    await provisionRole(tenant.id, tenantPassword);
    tenantDbUrl = tenantDatabaseUrl(tenant.id, tenantPassword);
  } catch (err) {
    // Roll back the row reservation so the next POST can retry.
    await db
      .delete(tenantBots)
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      )
      .catch(() => undefined);
    const message = err instanceof Error ? err.message : "unknown error";
    return Response.json(
      { error: `failed to provision tenant role: ${message}` },
      { status: 500 },
    );
  }

  // Now start the container.
  let started: Awaited<ReturnType<typeof startBot>>;
  try {
    started = await startBot({
      botId,
      tenantId: tenant.id,
      mode: mode as BotMode,
      decryptedSecrets,
      systemEnv: { DATABASE_URL: tenantDbUrl },
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

  // Container is up; record container id + running state. If THIS
  // step fails (transient DB error after the container started), we
  // must compensate by stopping/removing the container — otherwise
  // we leave an orphaned container with no DB row tracking it.
  try {
    const updated = await db
      .update(tenantBots)
      .set({
        containerId: started.id,
        isRunning: true,
        lastStartedAt: sql`now()`,
      })
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      )
      .returning();
    return Response.json({ bot: updated[0] });
  } catch (err) {
    // Compensating action: kill the container we just started so it
    // doesn't run untracked. Best-effort — log and continue if even
    // this fails (operator can clean up via `docker rm` later).
    try {
      const { stopBot } = await import("@/lib/bot-orchestrator");
      await stopBot(started.id);
    } catch (stopErr) {
      console.error(
        "[bots] compensating stop failed for orphaned container",
        started.id,
        stopErr,
      );
    }
    // Best-effort row cleanup as well.
    await db
      .delete(tenantBots)
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      )
      .catch(() => undefined);
    const message = err instanceof Error ? err.message : "unknown DB error";
    return Response.json(
      { error: `started container but DB update failed (rolled back): ${message}` },
      { status: 500 },
    );
  }
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
