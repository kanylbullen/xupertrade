import { LoginForm } from "@/components/login-form";
import { fetchAuthConfig } from "@/lib/auth";

export const dynamic = "force-dynamic";

const ERROR_MESSAGES: Record<string, string> = {
  "invalid-credentials": "Wrong username or password",
  "basic-auth-not-enabled": "Basic auth is not enabled",
  "bot-unreachable": "Backend is unreachable — try again in a moment",
  "oidc-misconfigured": "OIDC is enabled but not fully configured",
  "oidc-state-missing": "OIDC sign-in did not complete (state cookie lost)",
  "oidc-state-invalid": "OIDC sign-in failed (invalid state)",
  "oidc-token-exchange-failed": "OIDC token exchange failed — check provider settings",
  "oidc-no-claims": "OIDC provider returned no identity claims",
  "oidc-session-secret-unavailable":
    "Sign-in succeeded but the dashboard couldn't fetch its cookie-signing key — check API_KEY is set on both bot and dashboard",
  "tenant-disabled":
    "Your account has been disabled — contact the operator if you believe this is in error",
  "oidc-not-in-required-group":
    "Sign-in succeeded but your account isn't in the operator-approved group — ask the operator to grant access",
  "auth-locked":
    "Sign-in is unavailable — the dashboard can't read its auth configuration",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string; error?: string; fallback?: string }>;
}) {
  const params = await searchParams;
  const next = params.next ?? "/";
  const errorCode = params.error ?? "";
  const error = ERROR_MESSAGES[errorCode] || errorCode;
  const forceFallback = params.fallback === "basic";

  const cfg = await fetchAuthConfig(true);
  // Bot unreachable → render the login form anyway with the error
  // message so the user can see what's wrong rather than seeing a
  // blank page or crash.
  const oidcConfigured = !!cfg
    && cfg.mode === "oidc" && !!cfg.oidc_issuer && !!cfg.oidc_client_id;
  const basicAvailable = !!cfg && cfg.basic_user_set;

  // Show OIDC primary view only if mode=oidc, basic isn't being forced,
  // and OIDC is actually usable. Otherwise fall back to basic form.
  const showOidc =
    !!cfg && cfg.mode === "oidc" && oidcConfigured && !forceFallback;
  const oidcIssuer = cfg?.oidc_issuer ?? "";

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-bold tracking-tight">xupertrade</h1>
          <p className="text-sm text-muted-foreground mt-1">Sign in to continue</p>
        </div>

        {cfg?.mode === "locked" ? (
          <LockedNotice />
        ) : showOidc ? (
          <>
            <OidcLogin
              issuer={oidcIssuer}
              next={next}
              error={error}
            />
            {basicAvailable && (
              <div className="text-center">
                <a
                  href={`/login?fallback=basic${
                    next !== "/" ? `&next=${encodeURIComponent(next)}` : ""
                  }`}
                  className="text-xs text-muted-foreground hover:text-foreground underline"
                >
                  OIDC not working? Sign in with username + password
                </a>
              </div>
            )}
          </>
        ) : cfg?.mode === "oidc" && !oidcConfigured ? (
          <>
            <div className="space-y-4 rounded-lg border bg-card p-6">
              <p className="text-sm text-yellow-400">
                OIDC mode is enabled but not fully configured. Falling back to
                username + password if available.
              </p>
            </div>
            {basicAvailable ? (
              <LoginForm next={next} initialError={error} />
            ) : (
              <p className="text-sm text-muted-foreground text-center">
                No basic auth configured either — the operator can set one
                on the host with <code>scripts/set-basic-auth.sh</code>
              </p>
            )}
          </>
        ) : (
          <LoginForm next={next} initialError={error} />
        )}
      </div>
    </div>
  );
}

/**
 * Shown when `resolveMode` returned "locked": the stored auth mode is
 * gone on an installation that already has tenants, so we refuse to
 * serve anything rather than fall back to open access. Tells the
 * operator what happened and how to get out of it, without naming any
 * secret or its value.
 *
 * Every path listed here has to work from outside the dashboard:
 * Options → Authentication sits behind this same lock, and there is
 * no env var for a basic user — `getAuthConfig` reads only
 * `AUTH_MODE` and `OIDC_*` from env. The full procedure is in
 * CLAUDE.md § 3, "Dashboard auth recovery".
 */
function LockedNotice() {
  return (
    <div className="space-y-4 rounded-lg border border-red-500/40 bg-card p-6">
      <p className="text-sm font-medium text-red-400">
        Authentication is locked
      </p>
      <p className="text-sm text-muted-foreground">
        The dashboard could not read how this installation authenticates
        users. That normally means its Redis data was lost — a flush, or
        the data volume removed. Rather than fall back to open access,
        every page is withheld until the configuration is restored.
      </p>
      <div className="space-y-2 text-sm text-muted-foreground">
        <p>Operator, on the host — any one of:</p>
        <ul className="list-disc space-y-1 pl-5">
          <li>
            restore Redis from its{" "}
            <code className="font-mono">dump.rdb</code> snapshot;
          </li>
          <li>
            set <code className="font-mono">AUTH_MODE=oidc</code> with{" "}
            <code className="font-mono">OIDC_ISSUER</code>,{" "}
            <code className="font-mono">OIDC_CLIENT_ID</code> and{" "}
            <code className="font-mono">OIDC_CLIENT_SECRET</code> in the
            secrets manager, then recreate the dashboard container;
          </li>
          <li>
            run <code className="font-mono">scripts/set-basic-auth.sh</code>{" "}
            to set a username and password.
          </li>
        </ul>
      </div>
    </div>
  );
}

function OidcLogin({
  issuer,
  next,
  error,
}: {
  issuer: string;
  next: string;
  error: string;
}) {
  let host = issuer;
  try {
    host = new URL(issuer).host;
  } catch {
    // ignore
  }
  const startUrl = `/api/auth/oidc/start?next=${encodeURIComponent(next)}`;

  return (
    <div className="space-y-4 rounded-lg border bg-card p-6">
      {error && (
        <p className="text-sm text-red-400 border border-red-500/30 bg-red-500/5 rounded px-3 py-2">
          {error}
        </p>
      )}
      <p className="text-sm text-muted-foreground">
        You will be redirected to <span className="font-mono">{host}</span> to
        sign in.
      </p>
      <a
        href={startUrl}
        className="inline-flex w-full items-center justify-center rounded-md bg-foreground px-4 py-2 text-sm font-medium text-background transition-colors hover:opacity-90"
      >
        Sign in with {host}
      </a>
    </div>
  );
}
