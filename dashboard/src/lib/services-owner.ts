/**
 * The services owner: the one bot per tenant that runs the side services
 * (roadmap NU-7, operator decision 5.12) — the Telegram notifier and
 * command poller, HODL signal evaluation, the daily vault scanner and
 * the HL key-expiry reminders.
 *
 * It is the bot whose mode equals `HYPERTRADE_SERVICES_OWNER_MODE`
 * (default `paper`). Paper always runs, needs no exchange key and so
 * cannot be cleared by HyperLiquid, which lets testnet and mainnet be
 * stopped without silencing the alarms. The owner used to be mainnet:
 * stopping the real-money bot took Telegram, HODL and vaults with it.
 *
 * Every consumer reads the same function — the orchestrator's env gate
 * (`buildSpec` sets TELEGRAM_ENABLED + SERVICES_OWNER), the memory cap,
 * the /hodl and /vaults pages and send-unlock-link — so they cannot
 * disagree about which bot to talk to.
 *
 * A change reaches a bot only when that bot is restarted. Restart the
 * OLD owner first, then the new one: two bots polling Telegram
 * getUpdates with one token collide, and only one may send HODL and
 * vault alerts.
 *
 * Pure (no DB); the running-owner check lives in
 * `services-owner-check.ts` so the orchestrator stays DB-free.
 */

import type { BotMode } from "./bot-orchestrator";

export const SERVICES_OWNER_MODE_ENV = "HYPERTRADE_SERVICES_OWNER_MODE";
export const DEFAULT_SERVICES_OWNER_MODE: BotMode = "paper";

const MODES: readonly string[] = ["paper", "testnet", "mainnet"];
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

let warnedInvalidMode: string | null = null;
let warnedInvalidAddress = false;

/**
 * The owner mode. Unset or empty → paper. An unknown value also falls
 * back to paper, with one warning per value: every dashboard code path
 * reads this same function, so a typo still yields exactly one owner
 * rather than a failed bot start or a page crash.
 */
export function servicesOwnerMode(): BotMode {
  const raw = (process.env[SERVICES_OWNER_MODE_ENV] ?? "").trim().toLowerCase();
  if (!raw) return DEFAULT_SERVICES_OWNER_MODE;
  if (MODES.includes(raw)) return raw as BotMode;
  if (warnedInvalidMode !== raw) {
    warnedInvalidMode = raw;
    console.warn(
      `[services-owner] ${SERVICES_OWNER_MODE_ENV}=${JSON.stringify(raw)} ` +
        `is not paper, testnet or mainnet — using ${DEFAULT_SERVICES_OWNER_MODE}`,
    );
  }
  return DEFAULT_SERVICES_OWNER_MODE;
}

export function isServicesOwner(mode: BotMode): boolean {
  return mode === servicesOwnerMode();
}

/** Log line shared by the orchestrator routes and the watchdog. */
export function servicesOwnerMissingMessage(tenantId: string): string {
  return (
    `no running ${servicesOwnerMode()} bot for tenant ${tenantId} — it is ` +
    "the services owner, so Telegram, HODL, the vault scanner and key " +
    "reminders are off. Start it, or point HYPERTRADE_SERVICES_OWNER_MODE " +
    "at a running mode and restart the old owner first."
  );
}

/**
 * The operator's vault-tracking wallet, from `VAULT_TRACKING_ADDRESS` in
 * the dashboard's env (Phase). The owner bot is paper by default and has
 * no mainnet account to fall back on, so /vaults lists holdings only
 * when this (or the tenant's own Credentials slot) is set.
 *
 * Returns null when unset or not `0x` + 40 hex. The warning never
 * includes the value: it is a wallet address (CLAUDE.md § 0).
 */
export function operatorVaultTrackingAddress(): string | null {
  const raw = (process.env.VAULT_TRACKING_ADDRESS ?? "").trim();
  if (!raw) return null;
  if (ADDRESS_RE.test(raw)) return raw;
  if (!warnedInvalidAddress) {
    warnedInvalidAddress = true;
    console.warn(
      "[services-owner] VAULT_TRACKING_ADDRESS is not 0x + 40 hex — ignored",
    );
  }
  return null;
}

/** Test-only: re-arm the once-per-value warnings. */
export function _resetServicesOwnerWarningsForTests(): void {
  warnedInvalidMode = null;
  warnedInvalidAddress = false;
}
