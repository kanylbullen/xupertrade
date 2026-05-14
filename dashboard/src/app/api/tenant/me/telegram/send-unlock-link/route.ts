/**
 * POST /api/tenant/me/telegram/send-unlock-link  (PR 3c)
 *
 * Triggers the bot to DM the tenant a signed deeplink they can
 * click to unlock their stored credentials from outside the web
 * session (e.g. after the K-cache expired but they're on mobile).
 *
 * Flow:
 *   1. Require an authenticated tenant.

 *   2. Look up the tenant's linked Telegram chat (PR 3a/3b).
 *      412 if none — the tenant must run /link first.
 *   3. Find a running tenant-bot to act as the Telegram sender.
 *      We don't need a particular mode; any running tenant-bot
 *      has the same Telegram token (per `tenant_secrets`) and
 *      can DM the chat.
 *   4. Mint a short-lived signed unlock token + build the
 *      `/unlock?token=...` URL on PUBLIC_URL.
 *   5. POST it to the bot's `/api/internal/send-unlock-link`
 *      endpoint (API_KEY-gated), which calls TelegramNotifier.send.
 *
 * 503 if no running bot exists (can't DM without a sender).
 * 412 if Telegram is not linked.
 */

import { and, eq } from "drizzle-orm";

import { appendAuditLog } from "@/lib/audit-log";
import { getBotApiUrl } from "@/lib/bot-api";
import { loadBotApiKey } from "@/lib/bot-api-key";
import { db, tenantBots, tenantTelegramLinks } from "@/lib/db";
import { checkRateLimit } from "@/lib/rate-limit";
import { requireTenant } from "@/lib/tenant";
import { mintUnlockToken } from "@/lib/unlock-token";

const RATE_LIMIT_MAX = 5;          // 5 unlock-link DMs
const RATE_LIMIT_WINDOW_SEC = 900; // per 15 minutes

export const dynamic = "force-dynamic";

function getPublicBase(): string | null {
  const raw = (
    process.env.PUBLIC_URL ||
    process.env.DASHBOARD_URL ||
    ""
  ).trim().replace(/\/+$/, "");
  return raw || null;
}

export async function POST(req: Request): Promise<Response> {
  let tenant: Awaited<ReturnType<typeof requireTenant>>;
  try {
    tenant = await requireTenant(req);
  } catch (err) {
    if (err instanceof Response) return err;
    throw err;
  }

  // 0. Rate-limit before any work. Even an authed tenant could
  //    spam this endpoint and noise up their own Telegram chat;
  //    cap at 5/15min/tenant. Audit the denial so operator can
  //    spot a misbehaving caller.
  const rl = await checkRateLimit(
    "unlock-link-send",
    tenant.id,
    RATE_LIMIT_MAX,
    RATE_LIMIT_WINDOW_SEC,
  );
  if (!rl.allowed) {
    await appendAuditLog(tenant.id, "tenant", "telegram.unlock-link.rate-limited", {
      reset_in_seconds: rl.resetInSeconds,
    });
    return Response.json(
      {
        error: `rate limit: max ${RATE_LIMIT_MAX} unlock links per ${RATE_LIMIT_WINDOW_SEC / 60} min`,
        retryAfterSeconds: rl.resetInSeconds,
      },
      { status: 429, headers: { "Retry-After": String(rl.resetInSeconds) } },
    );
  }

  // 1. Telegram linked?
  const links = await db
    .select({ chatId: tenantTelegramLinks.telegramChatId })
    .from(tenantTelegramLinks)
    .where(eq(tenantTelegramLinks.tenantId, tenant.id))
    .limit(1);
  const link = links[0];
  if (!link) {
    return Response.json(
      {
        error:
          "telegram not linked — connect your Telegram chat on /settings/credentials first",
      },
      { status: 412 },
    );
  }

  // 2. Find a running tenant-bot to act as Telegram sender.
  const runningBots = await db
    .select()
    .from(tenantBots)
    .where(
      and(
        eq(tenantBots.tenantId, tenant.id),
        eq(tenantBots.isRunning, true),
      ),
    )
    .limit(1);
  const bot = runningBots[0];
  if (!bot) {
    return Response.json(
      {
        error:
          "no running bot — start one (paper / testnet / mainnet) so it can deliver the Telegram DM",
      },
      { status: 503 },
    );
  }
  const base = getBotApiUrl(bot);
  if (!base) {
    return Response.json(
      { error: "bot row has no container_name (unexpected)" },
      { status: 500 },
    );
  }

  // 3. Mint token + build URL.
  const publicBase = getPublicBase();
  if (!publicBase) {
    return Response.json(
      {
        error:
          "server misconfigured: PUBLIC_URL/DASHBOARD_URL not set",
      },
      { status: 500 },
    );
  }
  const token = await mintUnlockToken(tenant.id);
  const url = `${publicBase}/unlock?token=${encodeURIComponent(token)}`;

  // 4. Forward to the bot's internal endpoint.
  // Per security audit H-1: per-bot API key, not shared env var.
  const apiKey = (await loadBotApiKey(bot.id)) || "";
  let res: Response;
  try {
    res = await fetch(`${base}/api/internal/send-unlock-link`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(apiKey ? { "X-Api-Key": apiKey } : {}),
      },
      body: JSON.stringify({
        chat_id: link.chatId.toString(),
        url,
      }),
      // Short timeout — DM should be instant; if Telegram's API
      // is slow, the bot will surface the error and we 502.
      signal: AbortSignal.timeout(10_000),
    });
  } catch (e) {
    await appendAuditLog(tenant.id, "tenant", "telegram.unlock-link.failed", {
      reason: "bot-unreachable",
      bot_mode: bot.mode,
    });
    return Response.json(
      {
        error: `failed to reach bot: ${e instanceof Error ? e.message : String(e)}`,
      },
      { status: 502 },
    );
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    await appendAuditLog(tenant.id, "tenant", "telegram.unlock-link.failed", {
      reason: "bot-rejected",
      status: res.status,
      bot_mode: bot.mode,
    });
    return Response.json(
      { error: `bot rejected send: ${res.status} ${text}` },
      { status: 502 },
    );
  }
  await appendAuditLog(tenant.id, "tenant", "telegram.unlock-link.sent", {
    bot_mode: bot.mode,
  });
  return Response.json({ sent: true });
}
