"use client";

import { useEffect, useRef, useState, useTransition } from "react";

type Props = {
  /**
   * Called after a successful POST /api/tenant/me/unlock. The modal
   * closes itself before invoking — callers should refresh whatever
   * UI was waiting on K to be available.
   */
  onUnlocked: () => void;
  /** Called when the user dismisses without unlocking. Optional —
   *  if omitted, dismissal is disabled (modal blocks all interaction
   *  until passphrase is entered or the page is navigated away). */
  onCancel?: () => void;
};

/**
 * Modal that prompts for the tenant's passphrase and POSTs it to
 * /api/tenant/me/unlock to cache K in Redis for the session.
 *
 * Reusable across pages — credentials page mounts it when the
 * tenant is locked AND wants to set/replace a secret; future
 * bot-start flow (PR 2) and Telegram-deeplink unlock page (PR 3)
 * will also mount it.
 *
 * Renders a fixed-position overlay; consumers don't need to manage
 * portals or z-index.
 */
export function UnlockModal({ onUnlocked, onCancel }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Escape-to-dismiss when cancel is allowed.
  useEffect(() => {
    if (!onCancel) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel?.();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const value = inputRef.current?.value ?? "";
    if (!value) {
      setError("Passphrase required");
      return;
    }
    setError(null);
    startTransition(async () => {
      const res = await fetch("/api/tenant/me/unlock", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ passphrase: value }),
      });
      if (res.ok) {
        // Clear before calling onUnlocked so any re-render that
        // unmounts us doesn't leak the value via React state.
        if (inputRef.current) inputRef.current.value = "";
        onUnlocked();
        return;
      }
      const data = await res.json().catch(() => null);
      setError((data as { error?: string })?.error ?? `Unlock failed (${res.status})`);
    });
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="unlock-modal-title"
    >
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border bg-background p-6 shadow-lg"
      >
        <h2 id="unlock-modal-title" className="text-lg font-semibold">
          Unlock credentials
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Enter your passphrase to decrypt your stored credentials for this
          session.
        </p>
        <input
          ref={inputRef}
          type="password"
          name="passphrase"
          autoComplete="current-password"
          className="mt-4 w-full rounded border bg-background px-3 py-2 text-sm"
          placeholder="Passphrase"
          disabled={isPending}
        />
        {error && (
          <p className="mt-2 text-sm text-red-500" role="alert">
            {error}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              disabled={isPending}
              className="rounded border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              Cancel
            </button>
          )}
          <button
            type="submit"
            disabled={isPending}
            className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {isPending ? "Unlocking…" : "Unlock"}
          </button>
        </div>
      </form>
    </div>
  );
}
