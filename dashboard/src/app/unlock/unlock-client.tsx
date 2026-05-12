"use client";

import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

type Props = {
  tenantId: string;
  tenantLabel: string;
};

/**
 * Passphrase entry on /unlock — calls existing /api/tenant/me/unlock,
 * which also caches K in Redis for the session. After success we
 * redirect to /settings/bots so the user can start their bots.
 *
 * Requires an authenticated session — the unlock endpoint itself
 * gates on `requireTenant`. If the user isn't signed in we show
 * a "sign in first" message rather than silently 401'ing the
 * passphrase POST (less confusing).
 */
export function UnlockClient({ tenantId, tenantLabel }: Props) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [pending, startTransition] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const value = inputRef.current?.value ?? "";
    if (!value) {
      setError("Passphrase required");
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch("/api/tenant/me/unlock", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ passphrase: value }),
        });
        if (!res.ok) {
          // /api/tenant/me/unlock uses 401 for BOTH "not
          // authenticated" (requireTenant) and "wrong passphrase"
          // — disambiguate via the JSON error body so we don't
          // tell a tenant who typed the wrong passphrase to "sign
          // in first".
          const data = await res.json().catch(() => null);
          const apiError = (data as { error?: string })?.error;
          if (
            res.status === 401 &&
            apiError === "not authenticated"
          ) {
            setError(
              "Sign in first — open the dashboard, authenticate, then click the unlock link again.",
            );
            return;
          }
          setError(apiError ?? `Failed (${res.status})`);
          return;
        }
        if (inputRef.current) inputRef.current.value = "";
        setSuccess(true);
        // Brief pause so user sees the success state, then route
        // to /settings/bots where they likely want to be.
        setTimeout(() => {
          router.push("/settings/bots");
          router.refresh();
        }, 800);
      } catch (e) {
        setError(
          e instanceof Error
            ? `Network error: ${e.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  if (success) {
    return (
      <div className="rounded-lg border border-green-500/40 bg-green-500/10 p-6">
        <h2 className="text-lg font-semibold text-green-500">
          ✅ Unlocked
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          Redirecting to your bots…
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="space-y-4 rounded-lg border p-6">
      <p className="text-xs text-muted-foreground">
        Unlocking for{" "}
        <span className="font-medium text-foreground">{tenantLabel}</span>
      </p>
      <input
        ref={inputRef}
        type="password"
        name="passphrase"
        autoComplete="current-password"
        className="w-full rounded border bg-background px-3 py-2 text-sm"
        placeholder="Passphrase"
        disabled={pending}
        autoFocus
        // Pin tenant id into form data so any future logging can
        // tie the action to the deeplink subject (PR 3d audit log).
        data-tenant-id={tenantId}
      />
      {error && (
        <p className="text-sm text-red-500" role="alert">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={pending}
        className="w-full rounded bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {pending ? "Unlocking…" : "Unlock"}
      </button>
    </form>
  );
}
