/**
 * Tenant ↔ Telegram link lifecycle (PR 3a).
 *
 *   GET    /api/tenant/me/telegram/link
 *     → { linked: false } when no link exists, or
 *       { linked: true, chatId: string, username: string|null,
 *         linkedAt: ISO8601, lastUnlockAt: ISO8601|null }
 *     chatId is a string because Telegram chat IDs are BIGINT in
 *     the DB and JSON has no native bigint type.
 *
 *   POST   /api/tenant/me/telegram/link
 *     → { code: string, expiresInSeconds: number }
 *     Mints a 6-digit code (or returns the existing one if a
 *     non-expired code already exists for this tenant — prevents a
 *     malicious authenticated user from churning Redis keys by
 *     spamming the endpoint). Tenant then sends `/link <code>` to
 *     the bot to complete the pair.
 *
 *   DELETE /api/tenant/me/telegram/link
 *     → { unlinked: boolean }
 *     true when a row was actually removed, false when no link
 *     existed (still 200 — idempotent so the UI can blindly POST
 *     without checking state first).
 *
 * No passphrase/unlock needed for any of these — linking metadata
 * lives outside the encrypted secret material. The bot enforces
 * the second proof (chat-ownership) when consuming the code.
 *
 * Codes live in Redis under `tg-link:<code>` → tenantId with a
 * 10-min TTL. A reverse pointer `tg-link:tenant:<tenantId>` ->
 * code is kept in sync so we can return an existing active code
 * instead of churning. One-shot: bot's `/link` handler deletes
 * both keys after a successful upsert.
 */

import { eq } from "drizzle-orm";
import { randomInt } from "node:crypto";

import { db, tenantTelegramLinks } from "@/lib/db";
import { getRedisClient } from "@/lib/redis";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

const CODE_TTL_SECONDS = 600; // 10 minutes — enough to switch apps
const MAX_CODE_GENERATION_ATTEMPTS = 5;

function makeCode(): string {
  // 6-digit, zero-padded. randomInt is CSPRNG-backed; uniformly
  // distributed across 0..999_999. Leading zeros preserved because
  // the user types it into Telegram and the bot parses as a string.
  return String(randomInt(0, 1_000_000)).padStart(6, "0");
}

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
    .from(tenantTelegramLinks)
    .where(eq(tenantTelegramLinks.tenantId, tenant.id))
    .limit(1);
  const link = rows[0];
  if (!link) {
    return Response.json({ linked: false });
  }
  return Response.json({
    linked: true,
    // bigint → string for JSON (no native bigint in JSON).
    chatId: link.telegramChatId.toString(),
    username: link.telegramUsername,
    linkedAt: link.linkedAt,
    lastUnlockAt: link.lastUnlockAt,
  });
}

export async function POST(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const redis = getRedisClient();
  const reversePointerKey = `tg-link:tenant:${tenant.id}`;

  // If this tenant already has an active code, return it as-is
  // along with the remaining TTL — prevents a spam-POST from
  // churning Redis keys and gives the UI an idempotent feel.
  const existingCode = await redis.get(reversePointerKey);
  if (existingCode !== null) {
    const ttl = await redis.ttl(reversePointerKey);
    if (ttl > 0) {
      return Response.json({
        code: existingCode,
        expiresInSeconds: ttl,
      });
    }
    // ttl ≤ 0 means the key is expiring at this moment (race) —
    // fall through to mint a fresh code.
  }

  // Retry on collision (probability ~ 1e-6 per attempt). Use
  // SET NX so a parallel POST for a different tenant can't
  // accidentally overwrite our code.
  let code: string | null = null;
  for (let i = 0; i < MAX_CODE_GENERATION_ATTEMPTS; i++) {
    const candidate = makeCode();
    const set = await redis.set(
      `tg-link:${candidate}`,
      tenant.id,
      "EX",
      CODE_TTL_SECONDS,
      "NX",
    );
    if (set === "OK") {
      code = candidate;
      break;
    }
  }
  if (code === null) {
    // 5 collisions in a row is astronomically unlikely; surface
    // as 500 rather than retry forever.
    return Response.json(
      { error: "failed to generate unique code; please retry" },
      { status: 500 },
    );
  }

  // Set the reverse pointer with matching TTL so future POSTs from
  // this tenant can short-circuit to the same code. Bot's /link
  // handler will delete both keys after successful upsert.
  await redis.set(reversePointerKey, code, "EX", CODE_TTL_SECONDS);

  return Response.json({
    code,
    expiresInSeconds: CODE_TTL_SECONDS,
  });
}

export async function DELETE(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  const deleted = await db
    .delete(tenantTelegramLinks)
    .where(eq(tenantTelegramLinks.tenantId, tenant.id))
    .returning({ tenantId: tenantTelegramLinks.tenantId });

  return Response.json({
    unlinked: deleted.length > 0,
  });
}
