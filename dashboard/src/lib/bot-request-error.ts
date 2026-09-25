/**
 * The line /settings/bots shows when a create or start request fails.
 *
 * `bot-cap-exceeded` (`lib/admin/limits.ts:limitExceededResponse`) gets
 * its own text: since roadmap NU-8 a new tenant starts at
 * `max_active_bots = 0`, so the bare code would be the first thing
 * every invited user sees. Everything else keeps the server's `error`
 * string, or the status when the body had none.
 */
export function botRequestErrorText(body: unknown, status: number): string {
  const b = (body ?? {}) as { error?: unknown; limit?: unknown };
  if (b.error === "bot-cap-exceeded" && typeof b.limit === "number") {
    return b.limit === 0
      ? "Your account can't run bots yet — ask the operator to raise your bot limit."
      : `You're at your bot limit (${b.limit}) — stop a running bot, or ask the operator to raise the limit.`;
  }
  return typeof b.error === "string" ? b.error : `Failed (${status})`;
}
