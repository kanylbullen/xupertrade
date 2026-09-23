/**
 * `?next=` redirect-target validation, shared by the server (OIDC
 * callback, login redirect) and the client (`login-form.tsx`).
 *
 * Deliberately dependency-free and NOT `server-only`: the login form
 * is a Client Component and had its own copy of these rules, which
 * meant the same bug had to be found twice. One implementation, one
 * place to fix.
 */

/** A same-origin base used only to resolve `next` for the origin
 *  check. The real redirect is built from PUBLIC_URL by the caller;
 *  this base just has to be a valid absolute URL that `next` must not
 *  escape. `.invalid` is reserved (RFC 2606) and never resolvable. */
const PROBE_BASE = "https://safe-next.invalid/";

/**
 * Reject hostile or non-sensical redirect targets after login.
 *
 * Returns the original string when it is a safe same-origin app path,
 * otherwise `"/"`.
 *
 * SECURITY (analysis-2026-09-15 § 5, Medium). The prefix checks alone
 * were not enough. The WHATWG URL parser treats a backslash as a path
 * separator under a special scheme, so `new URL("/\\evil.com", base)`
 * resolves to `https://evil.com/` — an open redirect reachable
 * straight off a *successful* sign-in, which is exactly when the user
 * has been conditioned to trust wherever they land. `"/\\"` was not
 * the only spelling either (`"/\\\\evil.com"` does the same), and the
 * browser applies the same normalisation to
 * `window.location.href = "/\\evil.com"`.
 *
 * Rather than enumerate spellings, resolve the candidate the way the
 * browser will and require the result to stay on the base origin. The
 * prefix rules are kept on top of that: they express intent the
 * origin check doesn't (don't bounce back to /login, don't land on a
 * JSON API route) and they reject relative paths the parser would
 * happily accept.
 */
export function safeNext(raw: string): string {
  if (!raw || !raw.startsWith("/")) return "/";
  // No protocol-relative or absolute URLs.
  if (raw.startsWith("//")) return "/";
  // Don't loop back to the login page itself.
  if (raw === "/login" || raw.startsWith("/login?")) return "/";
  // Don't redirect into API routes — they aren't user-facing pages.
  if (raw.startsWith("/api/")) return "/";

  // The authoritative check: whatever the URL parser makes of this, it
  // must land on our own origin.
  let resolved: URL;
  try {
    resolved = new URL(raw, PROBE_BASE);
  } catch {
    return "/";
  }
  if (resolved.origin !== new URL(PROBE_BASE).origin) return "/";
  // Re-apply the path rules to the RESOLVED path, so a spelling that
  // only reveals itself after parsing can't slip past them either.
  if (resolved.pathname === "/login") return "/";
  if (resolved.pathname.startsWith("/api/")) return "/";

  return raw;
}
