"use client";

import { useEffect, useRef, useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Mode = "disabled" | "basic" | "oidc";

type Config = {
  mode: Mode;
  basic_user_set: boolean;
  oidc_issuer: string;
  oidc_client_id: string;
  oidc_scopes: string;
  // True when `src/instrumentation.ts` is overwriting these keys from
  // Phase env at every container start — any edits saved here will be
  // reverted on next restart. Drives the warning banner below.
  phase_managed?: boolean;
};

export function AuthConfig() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  // Form state
  const [mode, setMode] = useState<Mode>("disabled");
  // Username + password use refs (uncontrolled) so password-manager autofill
  // works — Bitwarden sets DOM value directly and React's controlled-input
  // pattern would discard the change on next render.
  const userRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const [oidcIssuer, setOidcIssuer] = useState("");
  const [oidcClientId, setOidcClientId] = useState("");
  const [oidcClientSecret, setOidcClientSecret] = useState("");
  const [oidcScopes, setOidcScopes] = useState("openid profile email");
  const [savedMsg, setSavedMsg] = useState("");

  async function refresh() {
    try {
      const res = await fetch("/api/auth/config", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as Config;
      setCfg(data);
      setMode(data.mode);
      setOidcIssuer(data.oidc_issuer || "");
      setOidcClientId(data.oidc_client_id || "");
      setOidcScopes(data.oidc_scopes || "openid profile email");
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  function save() {
    setSavedMsg("");
    setError(null);
    startTransition(async () => {
      const body: Record<string, string> = { mode };
      if (mode === "basic") {
        const userVal = userRef.current?.value.trim() ?? "";
        const passVal = passwordRef.current?.value ?? "";
        if (userVal) body.basic_user = userVal;
        if (passVal) body.basic_password = passVal;
      }
      if (mode === "oidc") {
        body.oidc_issuer = oidcIssuer;
        body.oidc_client_id = oidcClientId;
        if (oidcClientSecret) body.oidc_client_secret = oidcClientSecret;
        body.oidc_scopes = oidcScopes;
      }
      try {
        const res = await fetch("/api/auth/configure", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = (await res.json()) as { ok?: boolean; error?: string };
        if (!data.ok) {
          setError(data.error || `HTTP ${res.status}`);
          return;
        }
        setSavedMsg("Saved");
        if (passwordRef.current) passwordRef.current.value = "";
        if (userRef.current) userRef.current.value = "";
        setOidcClientSecret("");
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (!cfg) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Authentication</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Loading…</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Authentication</CardTitle>
          <Badge
            variant="outline"
            className={
              cfg.mode === "disabled"
                ? "border-yellow-500 text-yellow-400"
                : "border-green-500 text-green-400"
            }
          >
            {cfg.mode === "disabled" ? "OPEN" : cfg.mode.toUpperCase()}
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground mt-2">
          When enabled, all dashboard pages require sign-in. Configuration is
          stored in Redis and applies immediately.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {cfg.phase_managed && (
          <div
            role="alert"
            data-testid="phase-managed-banner"
            className="rounded-md border border-yellow-500/50 bg-yellow-500/10 px-3 py-2 text-sm text-yellow-200"
          >
            Auth config is sourced from Phase secrets at container
            startup; edits here will be overwritten on next restart.
          </div>
        )}
        <div className="space-y-2">
          <label className="text-sm font-medium">Mode</label>
          <div className="flex gap-2">
            {(["disabled", "basic", "oidc"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                disabled={isPending}
                className={`px-3 py-1 text-sm rounded border transition-colors ${
                  mode === m
                    ? "bg-foreground text-background"
                    : "bg-background hover:bg-accent"
                }`}
              >
                {m === "disabled" && "Off"}
                {m === "basic" && "Username + password"}
                {m === "oidc" && "OIDC"}
              </button>
            ))}
          </div>
        </div>

        {mode === "basic" && (
          <form
            className="space-y-3 rounded-lg border p-4"
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            <p className="text-xs text-muted-foreground">
              Single user. Password stored hashed (bcrypt). Leave blank to
              keep the current value.
              {cfg.basic_user_set && (
                <span className="block mt-1 text-green-400">
                  ✓ A username/password is currently configured
                </span>
              )}
            </p>
            <div className="space-y-2">
              <label htmlFor="auth-username" className="text-xs font-medium">
                Username
              </label>
              <input
                id="auth-username"
                name="username"
                type="text"
                autoComplete="username"
                ref={userRef}
                defaultValue=""
                placeholder={cfg.basic_user_set ? "(unchanged)" : "admin"}
                className="w-full rounded border bg-background px-3 py-2 text-sm"
                disabled={isPending}
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="auth-password" className="text-xs font-medium">
                Password
              </label>
              <input
                id="auth-password"
                name="password"
                type="password"
                autoComplete="current-password"
                ref={passwordRef}
                defaultValue=""
                placeholder={cfg.basic_user_set ? "(unchanged)" : "min 8 chars"}
                className="w-full rounded border bg-background px-3 py-2 text-sm"
                disabled={isPending}
              />
            </div>
            {/* Hidden submit so password managers recognize this as a save form */}
            <button type="submit" className="sr-only" tabIndex={-1} aria-hidden="true">
              Save credentials
            </button>
          </form>
        )}

        {mode === "oidc" && (
          <div className="space-y-3 rounded-lg border p-4">
            <p className="text-xs text-muted-foreground">
              OAuth2 / OpenID Connect with PKCE. The dashboard redirects to
              your provider on /login. Set the redirect URI on the provider
              to: <code className="bg-background px-1 py-0.5 rounded">[your-dashboard-url]/api/auth/oidc/callback</code>
            </p>
            <input
              type="url"
              placeholder="Issuer URL (e.g. https://auth.example.com)"
              value={oidcIssuer}
              onChange={(e) => setOidcIssuer(e.target.value)}
              className="w-full rounded border bg-background px-3 py-2 text-sm"
            />
            <input
              type="text"
              placeholder="Client ID"
              value={oidcClientId}
              onChange={(e) => setOidcClientId(e.target.value)}
              className="w-full rounded border bg-background px-3 py-2 text-sm"
            />
            <input
              type="password"
              placeholder="Client Secret (leave blank to keep current)"
              value={oidcClientSecret}
              onChange={(e) => setOidcClientSecret(e.target.value)}
              className="w-full rounded border bg-background px-3 py-2 text-sm"
            />
            <input
              type="text"
              placeholder="Scopes"
              value={oidcScopes}
              onChange={(e) => setOidcScopes(e.target.value)}
              className="w-full rounded border bg-background px-3 py-2 text-sm"
            />
          </div>
        )}

        {error && (
          <p className="text-sm text-red-400 border border-red-500/30 bg-red-500/5 rounded px-3 py-2">
            {error === "Bot API returned 401"
              ? "API_KEY required — set the API_KEY env var on the bot to allow auth changes"
              : error}
          </p>
        )}
        {savedMsg && (
          <p className="text-sm text-green-400">{savedMsg}</p>
        )}

        <div className="flex items-center justify-between pt-2 border-t border-border/40">
          <Button onClick={save} disabled={isPending}>
            {isPending ? "Saving…" : "Save"}
          </Button>
          {cfg.mode !== "disabled" && (
            <button
              type="button"
              onClick={() =>
                startTransition(async () => {
                  await fetch("/api/auth/logout", { method: "POST" }).catch(() => null);
                  window.location.href = "/login";
                })
              }
              disabled={isPending}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors"
            >
              Sign out (current session)
            </button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
