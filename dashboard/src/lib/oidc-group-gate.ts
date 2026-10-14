/**
 * The OIDC invite gate (security audit M-3, roadmap NU-8 point 2).
 *
 * When `OIDC_REQUIRED_GROUP` is set, a session whose `groups` claim
 * does not contain that group gets no tenant row: autocreate refuses
 * (`lib/tenant.ts:autocreateTenant`). Existing tenants are never
 * re-checked. The OIDC callback only carries the claim into the
 * session cookie (`extractGroupsClaim`); the decision is made here.
 *
 * The match is exact and case-sensitive. `Hypertrade-Users` does not
 * satisfy `hypertrade-users`: IdPs such as Authentik treat group names
 * as distinct strings, so folding case could let a differently-named
 * group through, and a mismatch fails closed (the user is refused,
 * the operator sees the 403 body naming the required group).
 *
 * The gate existed in code before the variable reached the container:
 * `docker-compose.yml` did not pass it through, so every user the IdP
 * authenticated got a tenant. `warnIfOidcGroupGateOff` makes that
 * state loud at boot instead of silent.
 *
 * Kept free of DB and Redis imports so the boot check can load it
 * without pulling either in; the auth config is read lazily.
 */

import type { AuthConfig } from "./auth";

/**
 * The operator-configured required group, trimmed. Empty = no
 * enforcement: any successful OIDC sign-in autocreates a tenant.
 */
export function getRequiredOidcGroup(): string {
  return (process.env.OIDC_REQUIRED_GROUP || "").trim();
}

/**
 * Does a session's `groups` claim contain `requiredGroup`?
 *
 * `groups` is typed `unknown` on purpose: `verifySession` does not
 * validate the payload shape, and a string that slipped through would
 * turn `.includes` into a substring match ("not-admin" contains
 * "admin"). Only a real array counts; anything else is "no groups".
 */
export function hasRequiredGroup(groups: unknown, requiredGroup: string): boolean {
  const list = Array.isArray(groups) ? groups : [];
  return list.includes(requiredGroup);
}

type GateConfig = Pick<AuthConfig, "mode" | "oidc_issuer" | "oidc_client_id">;

/**
 * The boot warning to log, or null when there is nothing to warn about.
 *
 * Warns when the group is empty and OIDC sign-in can mint sessions.
 * That is wider than `mode === "oidc"`: the callback serves any mode
 * but `locked` once an issuer and client id are configured, so a
 * `basic` install with OIDC fields set onboards IdP users too. An
 * unreadable config (`null`) warns as well — boot is when Redis is
 * most likely to be late, and a spurious line costs less than a
 * missed one.
 */
export function oidcGroupGateWarning(
  cfg: GateConfig | null,
  requiredGroup: string,
): string | null {
  if (requiredGroup) return null;
  const oidcReachable =
    cfg === null ||
    cfg.mode === "oidc" ||
    Boolean(cfg.oidc_issuer && cfg.oidc_client_id);
  if (!oidcReachable) return null;
  const why =
    cfg === null
      ? "the auth config could not be read, so OIDC sign-in may be configured"
      : "OIDC sign-in is configured";
  return (
    `[oidc-group-gate] WARNING: ${why} and OIDC_REQUIRED_GROUP is empty — ` +
    "every user the IdP authenticates gets a tenant on first sign-in. " +
    "Set OIDC_REQUIRED_GROUP in Phase and recreate the dashboard " +
    "(docs/INVITE_ONBOARDING.md)."
  );
}

async function defaultReadConfig(): Promise<GateConfig | null> {
  const { fetchAuthConfig } = await import("./auth");
  return fetchAuthConfig(true);
}

/**
 * Boot check, called once from `instrumentation.ts`. Logs a WARNING
 * when the gate is off while OIDC can sign people in, and a one-line
 * confirmation naming the group when it is on. Never throws: a boot
 * log line must not be able to stop the dashboard from starting.
 * Returns whether the warning was logged (for tests).
 */
export async function warnIfOidcGroupGateOff(
  readConfig: () => Promise<GateConfig | null> = defaultReadConfig,
): Promise<boolean> {
  const requiredGroup = getRequiredOidcGroup();
  if (requiredGroup) {
    // JSON-quoted so stray case or inner whitespace is visible in the
    // log. The name is not a secret: the 403 body already returns it.
    console.log(
      `[oidc-group-gate] new tenants require OIDC group ${JSON.stringify(requiredGroup)}`,
    );
    return false;
  }
  let cfg: GateConfig | null;
  try {
    cfg = await readConfig();
  } catch {
    cfg = null;
  }
  const warning = oidcGroupGateWarning(cfg, requiredGroup);
  if (warning === null) return false;
  console.warn(warning);
  return true;
}
