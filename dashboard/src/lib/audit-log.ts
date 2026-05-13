/**
 * Tenant audit log helper (PR 3d).
 *
 * Thin wrapper around the existing `tenant_audit_log` table.
 * Centralises the call shape so individual routes don't repeat
 * the Drizzle insert + context-JSON-stringify dance, and gives us
 * one place to add buffering / backpressure / Sentry hooks later.
 *
 * All writes are best-effort: a DB blip when audit-logging an
 * unlock attempt shouldn't prevent the unlock itself. Callers
 * are expected to fire-and-await but the helper swallows errors
 * (logged via console.warn — the dashboard's std log target).
 */

import { db, tenantAuditLog } from "./db";

export type AuditActor = "tenant" | "operator";

export type AuditAction =
  // Existing actions any caller could write (extend as needed).
  | "secret.set"
  | "secret.delete"
  | "bot.start"
  | "bot.stop"
  | "bot.create"
  | "bot.delete"
  | "passphrase.set"
  | "passphrase.unlock"
  | "passphrase.unlock-failed"
  | "passphrase.unlock-rate-limited"
  | "passphrase.lock"
  | "tenant.disabled"
  // PR 3d: telegram linking + unlock-deeplink flow.
  | "telegram.link.code-minted"
  | "telegram.link.created"
  | "telegram.unlink"
  | "telegram.unlock-link.sent"
  | "telegram.unlock-link.rate-limited"
  | "telegram.unlock-link.failed";

/**
 * Append-only audit log write. Context is a free-form object
 * (Postgres TEXT column holds JSON.stringify(context)). Never
 * include secret values — only metadata about what happened.
 */
export async function appendAuditLog(
  tenantId: string,
  actor: AuditActor,
  action: AuditAction,
  context: Record<string, unknown> = {},
): Promise<void> {
  try {
    await db.insert(tenantAuditLog).values({
      tenantId,
      actor,
      action,
      contextJson: JSON.stringify(context),
    });
  } catch (err) {
    // Best-effort: never let an audit-write failure surface to the
    // caller. The action itself already happened (or already failed
    // for its own reasons); the audit-log miss is just a missing
    // trace — fix-forward via Sentry/monitoring rather than
    // failing the user-visible action.
    console.warn(
      "[audit-log] write failed",
      { tenantId, actor, action, error: err instanceof Error ? err.message : String(err) },
    );
  }
}
