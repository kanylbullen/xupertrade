/**
 * Tenant ↔ Telegram link lifecycle (PR 3a).
 *
 *   GET    /api/tenant/me/telegram/link  → { linked, chatId?, username?, linkedAt? }
 *   POST   /api/tenant/me/telegram/link  → { code: string }   creates a 6-digit
 *                                                              code valid 10 min;
 *                                                              tenant sends
 *                                                              `/link <code>` to
 *                                                              the bot to complete.
 *   DELETE /api/tenant/me/telegram/link  → { unlinked: true }  removes the link.
 *
 * No passphrase/unlock needed for any of these — linking metadata
 * lives outside the encrypted secret material. The bot enforces
 * the second proof (chat-ownership) when consuming the code.
 *
 * Codes live in Redis under `tg-link:<code>` → tenantId with a
 * 10-min TTL. One-shot: bot's `/link` handler deletes the key
 * after successful upsert.
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
  // we type-it-yourself in Telegram and the bot parses as a string.
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
    chatId: link.telegramChatId,
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
