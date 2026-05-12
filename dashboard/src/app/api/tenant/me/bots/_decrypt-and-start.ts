/**
 * Shared helper for decrypting tenant_secrets + starting a bot
 * container + persisting the resulting container_id back. Used by
 * both `POST /api/tenant/me/bots` (create new) and
 * `POST /api/tenant/me/bots/[id]/start` (restart existing).
 *
 * Caller is responsible for ensuring the `tenant_bots` row already
 * exists with the right id+mode, and for any pre-checks (multi-bot
 * gate, required-secrets check). This helper:
 *   1. requireUnlockedKey + decrypt all tenant_secrets
 *   2. provision/refresh the tenant pg role
 *   3. startBot()
 *   4. update tenant_bots with containerId + isRunning + lastStartedAt
 *   5. compensate (stop container) on step-4 failure
 *
 * Returns either a Response (to short-circuit the caller's handler)
 * or the updated bot row.
 */

import { and, eq, sql } from "drizzle-orm";

import { decryptSecret } from "@/lib/crypto/secrets";
import { db, tenantBots, tenantSecrets } from "@/lib/db";
import {
  type BotMode,
  getOrchestratorSystemEnv,
  startBot,
  stopBot,
} from "@/lib/bot-orchestrator";
import {
  generateRolePassword,
  provisionRole,
  tenantDatabaseUrl,
} from "@/lib/tenant-pg-role";
import { requireUnlockedKey, type Tenant } from "@/lib/tenant";

type Args = {
  req: Request;
  tenant: Tenant;
  botId: string;
  mode: BotMode;
};

type Result =
  | { kind: "response"; response: Response }
  | { kind: "ok"; bot: typeof tenantBots.$inferSelect };

export async function decryptAndStart(args: Args): Promise<Result> {
  const { req, tenant, botId, mode } = args;

  // 1. Unlock K and decrypt all tenant_secrets.
  let k: Buffer;
  try {
    k = await requireUnlockedKey(req, tenant);
  } catch (err) {
    if (err instanceof Response) return { kind: "response", response: err };
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
      // GCM auth-tag mismatch — wrong K (passphrase changed) or
      // tampered ciphertext. Map to 401 so the client re-unlocks.
      return {
        kind: "response",
        response: Response.json(
          {
            error: `failed to decrypt secret '${row.key}' — re-unlock with current passphrase`,
          },
          { status: 401 },
        ),
      };
    }
  }

  // 2. Provision (or rotate) the tenant's pg role.
  const tenantPassword = generateRolePassword();
  let tenantDbUrl: string;
  try {
    await provisionRole(tenant.id, tenantPassword);
    tenantDbUrl = tenantDatabaseUrl(tenant.id, tenantPassword);
  } catch (err) {
    const message = err instanceof Error ? err.message : "unknown error";
    return {
      kind: "response",
      response: Response.json(
        { error: `failed to provision tenant role: ${message}` },
        { status: 500 },
      ),
    };
  }

  // 3. Start the container.
  let started: Awaited<ReturnType<typeof startBot>>;
  try {
    started = await startBot({
      botId,
      tenantId: tenant.id,
      mode,
      decryptedSecrets,
      systemEnv: {
        ...getOrchestratorSystemEnv(),
        DATABASE_URL: tenantDbUrl,
      },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "unknown docker error";
    return {
      kind: "response",
      response: Response.json(
        { error: `failed to start container: ${message}` },
        { status: 500 },
      ),
    };
  }

  // 4. Persist the container_id. If THIS fails, compensate by
  // stopping the container so we don't leave it orphaned.
  try {
    const updated = await db
      .update(tenantBots)
      .set({
        containerId: started.id,
        isRunning: true,
        lastStartedAt: sql`now()`,
        // Clear lastStoppedAt so the timeline reads "started at X"
        // without a stale "stopped at Y" hanging around.
        lastStoppedAt: null,
      })
      .where(
        and(eq(tenantBots.id, botId), eq(tenantBots.tenantId, tenant.id)),
      )
      .returning();
    if (updated.length === 0) {
      // Row vanished between start and update — race with DELETE.
      // Compensate.
      try {
        await stopBot(started.id);
      } catch (stopErr) {
        console.error(
          "[bots] compensating stop failed for orphaned container",
          started.id,
          stopErr,
        );
      }
      return {
        kind: "response",
        response: Response.json(
          { error: "bot row was deleted during start (race)" },
          { status: 409 },
        ),
      };
    }
    return { kind: "ok", bot: updated[0] };
  } catch (err) {
    try {
      await stopBot(started.id);
    } catch (stopErr) {
      console.error(
        "[bots] compensating stop failed for orphaned container",
        started.id,
        stopErr,
      );
    }
    const message = err instanceof Error ? err.message : "unknown DB error";
    return {
      kind: "response",
      response: Response.json(
        {
          error: `started container but DB update failed (container stopped to avoid orphan): ${message}`,
        },
        { status: 500 },
      ),
    };
  }
}
