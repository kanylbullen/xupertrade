/**
 * Shared `Mode` type + URL helper. After PR #105 (sidebar cutover)
 * killed `lib/use-mode.ts`, several components inlined their own
 * `Mode` + `withMode` copies. Copilot review caught the drift risk —
 * this module is the single source of truth.
 *
 * Client-safe (pure, no Node imports). Import freely from any
 * component or page.
 */

export type Mode = "paper" | "testnet" | "mainnet";

export const MODES: readonly Mode[] = ["paper", "testnet", "mainnet"] as const;

export function isValidMode(v: string | null | undefined): v is Mode {
  return v === "paper" || v === "testnet" || v === "mainnet";
}

/**
 * Append `?mode=<mode>` to a path. Pass through if the path already
 * has a query string (uses `&` then). Used by per-bot API calls that
 * still rely on `?mode=` query routing in `lib/bot-api.ts:parseMode`.
 *
 * Mirrors the old `lib/use-mode.ts:withMode` semantics so deleting
 * the old file doesn't break behavior — just removes the duplication.
 */
export function withMode(path: string, mode: Mode): string {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}mode=${mode}`;
}
