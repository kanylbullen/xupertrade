/**
 * Resolve the client IP for rate-limit / audit purposes.
 *
 * Priority:
 *   1. `CF-Connecting-IP` — set by Cloudflare for tunnel traffic
 *      (cloudflared in `docker-compose.yml`). CF strips inbound
 *      copies, so this is trustworthy when present.
 *   2. Right-most `X-Forwarded-For` value when one or more proxies
 *      are in front of us. The right-most entry is the one our
 *      direct upstream (Caddy / cloudflared) added; everything to
 *      its left could be attacker-supplied. NOTE: this assumes
 *      exactly one trusted proxy hop. Two stacked trusted proxies
 *      would need `xff[xff.length - 2]`.
 *   3. `X-Real-IP` — Caddy's `reverse_proxy` sets this by default.
 *   4. `"unknown"` — never throw; rate-limit + audit must always
 *      have a key.
 *
 * Copilot review fix on PR #92: previous implementation took the
 * LEFT-most XFF entry, which is attacker-controlled when the proxy
 * appends rather than overwrites. That allowed per-IP rate-limit
 * bypass by spoofing the `X-Forwarded-For` header.
 */
export function getClientIp(req: Request): string {
  const cf = req.headers.get("cf-connecting-ip");
  if (cf && cf.trim()) return cf.trim();

  const xff = req.headers.get("x-forwarded-for");
  if (xff) {
    const parts = xff.split(",").map((s) => s.trim()).filter(Boolean);
    if (parts.length > 0) return parts[parts.length - 1];
  }

  const realIp = req.headers.get("x-real-ip");
  if (realIp && realIp.trim()) return realIp.trim();

  return "unknown";
}
