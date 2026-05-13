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
 *     Mints a 10-character Crockford-base32 code (≈10^15 keyspace,
 *     up from the original 10^6 6-digit code — M-1 fix) so
 *     brute-force against the bot's `/link` handler is
 *     computationally infeasible regardless of throttle. Returns
 *     the existing active code on repeat POSTs to avoid churning
 *     Redis keys. Also rate-limited to 10 mints per hour per
 *     tenant — each code is one-shot so this only stops abuse,
 *     not legitimate retries.
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
import { randomBytes } from "node:crypto";

import { appendAuditLog } from "@/lib/audit-log";
import { db, tenantTelegramLinks } from "@/lib/db";
import { checkRateLimit } from "@/lib/rate-limit";
import { getRedisClient } from "@/lib/redis";
import { requireTenant } from "@/lib/tenant";

export const dynamic = "force-dynamic";

const CODE_TTL_SECONDS = 600; // 10 minutes — enough to switch apps
const MAX_CODE_GENERATION_ATTEMPTS = 5;
const CODE_LENGTH = 10;
// Mint rate-limit: codes are one-shot + 10-min TTL, so 10/hr per
// tenant is generous for real users (lost-phone re-mint, switch
// devices) while blocking key churn from a compromised session.
const MINT_MAX_PER_HOUR = 10;
const MINT_WINDOW_SECONDS = 3600;

// Crockford base32 minus 0/1/I/O (4 most-confused glyphs). 32-char
// alphabet matches the regex /^[A-HJ-NP-Z2-9]{10}$/ — bot's `/link`
// parser validates with the same regex so codes round-trip.
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const CODE_PATTERN = /^[A-HJ-NP-Z2-9]{10}$/;

function makeCode(): string {
  // CSPRNG-backed. Alphabet length is 32 = 2^5, so a byte's low 5
  // bits map uniformly into the alphabet with zero bias — no
  // rejection sampling needed.
  const buf = randomBytes(CODE_LENGTH);
  let out = "";
  for (let i = 0; i < CODE_LENGTH; i++) {
    out += CODE_ALPHABET[buf[i] & 0x1f];
  }
  return out;
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

  // Idempotent fast-path FIRST (Copilot review fix on PR #95): if the
  // tenant already has an active code in the new format, return it
  // without consuming mint quota. Repeated POSTs from a polling UI
  // shouldn't 429.
  const existingCode = await redis.get(reversePointerKey);
  if (existingCode !== null && CODE_PATTERN.test(existingCode)) {
    const ttl = await redis.ttl(reversePointerKey);
    if (ttl > 0) {
      return Response.json({
        code: existingCode,
        expiresInSeconds: ttl,
      });
    }
    // ttl ≤ 0 means the key is expiring at this moment (race) —
    // fall through to mint a fresh code.
  } else if (existingCode !== null) {
    // Stale legacy 6-digit code (or some other non-Crockford value)
    // from before the PR #95 format change. Drop it so we mint a
    // fresh one. The bot would have rejected it anyway.
    await redis.del(reversePointerKey);
  }

  // Per-tenant mint rate-limit (M-1). Only applies when we're
  // actually minting a new code (the idempotent fast-path above
  // returns first). Defence-in-depth against a compromised
  // dashboard session minting unbounded codes to feed a brute-force
  // run against /link — even with the 32^10 keyspace making brute
  // force infeasible, key churn is still abuse.
  const limit = await checkRateLimit(
    "tg-link-mint",
    tenant.id,
    MINT_MAX_PER_HOUR,
    MINT_WINDOW_SECONDS,
  );
  if (!limit.allowed) {
    return Response.json(
      { error: "too many code-mint attempts; please try again later" },
      {
        status: 429,
        headers: { "Retry-After": String(limit.resetInSeconds) },
      },
    );
  }

  // Retry on collision (probability ~ 32^-10 per attempt — basically
  // never, but the loop is cheap and the SET NX guarantees no
  // overwrite of another tenant's in-flight code).
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

  await appendAuditLog(tenant.id, "tenant", "telegram.link.code-minted");

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

  if (deleted.length > 0) {
    await appendAuditLog(tenant.id, "tenant", "telegram.unlink");
  }

  return Response.json({
    unlinked: deleted.length > 0,
  });
}
