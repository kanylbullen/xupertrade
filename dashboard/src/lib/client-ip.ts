import { isIP } from "node:net";

/**
 * Resolve the client IP for rate-limit / audit purposes.
 *
 * Priority:
 *   1. `CF-Connecting-IP` — set by Cloudflare for tunnel traffic.
 *      Cloudflare strips inbound copies, cloudflared reaches
 *      `dashboard:3000` directly over the compose network, and Caddy
 *      now deletes the header on the LAN path (`caddy/Caddyfile` and
 *      `lib/caddy-admin.ts`), so every route that can deliver it is
 *      one we control.
 *   2. Right-most `X-Forwarded-For` value. Caddy SETs this header to
 *      the address it observed, so there is normally exactly one
 *      entry. Right-most rather than left-most is deliberate: if the
 *      set ever reverts to an append, the right-most entry is still
 *      the one our own upstream added and everything to its left is
 *      attacker-supplied (that was PR #92's bug). Under a set the two
 *      readings agree, so right-most is never the worse choice.
 *   3. `X-Real-IP` — Caddy's `reverse_proxy` sets this by default.
 *   4. `"unknown"` — never throw; rate-limit + audit must always have
 *      a key.
 *
 * Every candidate must parse as an IPv4 or IPv6 literal before it is
 * accepted. A header that survives all of the above but isn't an
 * address is not a client IP, and letting arbitrary text through
 * would put attacker-chosen text inside a Redis rate-limit key
 * (analysis-2026-09-15 § 5, Medium).
 */
export function getClientIp(req: Request): string {
  const cf = req.headers.get("cf-connecting-ip");
  if (cf && isIP(cf.trim()) !== 0) return cf.trim();

  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const parts = xff.split(",").map((s) => s.trim()).filter(Boolean);
    // Only the right-most entry is considered. If it doesn't parse,
    // fall through rather than walking left — every entry to the left
    // is client-supplied, so "our upstream wrote something odd" must
    // not become "trust the attacker's value instead".
    const last = parts[parts.length - 1];
    if (last && isIP(last) !== 0) return last;
  }

  const realIp = req.headers.get("x-real-ip");
  if (realIp && isIP(realIp.trim()) !== 0) return realIp.trim();

  return "unknown";
}
