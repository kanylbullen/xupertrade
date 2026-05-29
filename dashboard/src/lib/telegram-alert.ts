/**
 * Operator-level Telegram alerting (feat/heartbeat-watchdog).
 *
 * Sends a message DIRECTLY to the Telegram Bot HTTP API using the
 * operator's `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (injected into
 * the dashboard container by `phase run`, see docker-compose.yml).
 *
 * Why operator-level and not per-tenant: the per-tenant bot tokens live
 * in encrypted `tenant_secrets` (AES-256-GCM under the tenant's
 * passphrase / K-cache) and the dashboard has no plaintext access to
 * them. A heartbeat watchdog is a system/operator concern — it must be
 * able to alert when a bot is DEAD, which is exactly when that bot's own
 * Telegram notifier can't fire. So it uses the operator's Phase-sourced
 * token and talks to Telegram itself. Per-tenant alerting (each tenant's
 * own token) is a future extension; today it's operator-level.
 *
 * Fail-safe: this never throws. Missing token → warn + return `false`.
 * Fetch error / non-2xx → caught + logged, returns `false`. A confirmed
 * 2xx returns `true`. Alerting must never crash the watchdog loop; the
 * boolean lets the caller (heartbeat-watchdog) gate state changes — e.g.
 * only clearing a recovery dedup key once the alert is confirmed sent.
 *
 * Escaping: callers pass PRE-ESCAPED HTML. We send with
 * `parse_mode: "HTML"` so callers that want bold/emoji can include tags,
 * but any dynamic substring (tenant name, mode) must be run through
 * `escapeTelegramHtml` by the caller first. We do NOT double-escape here.
 */
import "server-only";

const TELEGRAM_API_BASE = "https://api.telegram.org"; // gitleaks:allow — public Telegram Bot API host, not operator infra

/** Escape the five characters Telegram's HTML parse mode treats specially. */
export function escapeTelegramHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Send an operator alert via Telegram. `text` must already be
 * HTML-escaped where it contains dynamic content (see module docstring).
 * Resolves regardless of outcome — errors are logged, never thrown.
 *
 * Returns `true` only on a confirmed 2xx from the Telegram API; returns
 * `false` when the token/chat is unconfigured, the request throws, or
 * Telegram responds non-2xx. Callers can use this to gate state changes
 * (see heartbeat-watchdog's recovery dedup-key handling).
 */
export async function sendOperatorTelegramAlert(
  text: string,
): Promise<boolean> {
  const token = (process.env.TELEGRAM_BOT_TOKEN || "").trim();
  const chatId = (process.env.TELEGRAM_CHAT_ID || "").trim();

  if (!token || !chatId) {
    console.warn(
      "[telegram-alert] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset — " +
        "skipping operator alert (graceful degradation)",
    );
    return false;
  }

  try {
    const res = await fetch(
      `${TELEGRAM_API_BASE}/bot${token}/sendMessage`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: chatId,
          text,
          parse_mode: "HTML",
        }),
        signal: AbortSignal.timeout(8000),
      },
    );
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      console.warn(
        `[telegram-alert] Telegram API returned ${res.status}: ${body.slice(0, 200)}`,
      );
      return false;
    }
    return true;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`[telegram-alert] send failed: ${msg}`);
    return false;
  }
}
