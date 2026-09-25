/**
 * The services owner: the one bot per tenant that runs the side services
 * (roadmap NU-7, operator decision 5.12) — the Telegram notifier and
 * command poller, HODL signal evaluation, the daily vault scanner and
 * the HL key-expiry reminders.
 *
 * For the operator tenant it is the bot whose mode equals
 * `HYPERTRADE_SERVICES_OWNER_MODE` (default `paper`). Paper always runs,
 * needs no exchange key and so cannot be cleared by HyperLiquid, which
 * lets testnet and mainnet be stopped without silencing the alarms.
 *
 * Every other tenant keeps the owner it had before NU-7, mainnet
 * (`servicesOwnerModeFor`). Event channels and control keys are not
 * tenant-scoped yet, and a paper bot needs no exchange key, so moving
 * the owner to paper for every tenant would hand a Telegram notifier to
 * any tenant who creates a paper bot.
 *
 * A change reaches a bot only when that bot is restarted. Restart the
 * OLD owner first, then the new one: two bots polling Telegram
 * getUpdates with one token collide. `buildSpec` labels each container
 * with what it was started as (`SERVICES_OWNER_LABEL`), and
 * `services-owner-check.ts` (the DB side) reads those labels.
 */

import type { BotMode } from "./bot-orchestrator";

export const SERVICES_OWNER_MODE_ENV = "HYPERTRADE_SERVICES_OWNER_MODE";
export const DEFAULT_SERVICES_OWNER_MODE: BotMode = "paper";
/** The owner of every non-operator tenant, as before NU-7. */
export const LEGACY_SERVICES_OWNER_MODE: BotMode = "mainnet";
/** Container label `buildSpec` sets to "true" or "false". */
export const SERVICES_OWNER_LABEL = "hypertrade.services_owner";

const MODES: readonly string[] = ["paper", "testnet", "mainnet"];
const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;

let warnedInvalidMode: string | null = null;
let warnedInvalidAddress = false;

/**
 * The operator's owner mode. Unset or empty → paper. An unknown value
 * also falls back to paper, with one warning per value: every dashboard
 * code path reads this same function, so a typo still yields exactly
 * one owner rather than a failed bot start or a page crash.
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

type TenantLike = { isOperator?: boolean | null };

/** The owner mode for one tenant (see the module comment). */
export function servicesOwnerModeFor(tenant: TenantLike): BotMode {
  return tenant.isOperator === true ? servicesOwnerMode() : LEGACY_SERVICES_OWNER_MODE;
}

export function isServicesOwner(mode: BotMode, tenant: TenantLike): boolean {
  return mode === servicesOwnerModeFor(tenant);
}

/**
 * Whether a container was started as the services owner, read from its
 * labels. A container without the label was spawned before NU-7, when
 * the orchestrator gave Telegram to the mainnet bot only.
 */
export function containerIsServicesOwner(labels: Record<string, string>): boolean {
  const label = labels[SERVICES_OWNER_LABEL];
  if (label !== undefined) return label === "true";
  return labels["hypertrade.mode"] === LEGACY_SERVICES_OWNER_MODE;
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
