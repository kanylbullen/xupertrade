"use client";

import { useRef, useState, useTransition } from "react";

import { Dialog } from "@base-ui/react/dialog";

type Props = {
  /**
   * Called after a successful POST /api/tenant/me/unlock. The modal
   * closes itself before invoking — callers should refresh whatever
   * UI was waiting on K to be available.
   */
  onUnlocked: () => void;
  /** Called when the user dismisses without unlocking. Optional —
   *  if omitted, dismissal via Escape / backdrop click is disabled
   *  (modal blocks all interaction until passphrase is entered or
   *  the page is navigated away). */
  onCancel?: () => void;
};

/**
 * Modal that prompts for the tenant's passphrase and POSTs it to
 * /api/tenant/me/unlock to cache K in Redis for the session.
 *
 * Uses Base UI's Dialog primitive so we get focus trap, focus
 * restoration, ARIA wiring, and Escape handling for free.
 *
 * Reusable across pages — credentials page mounts it when the
 * tenant is locked AND wants to set/replace a secret; future
 * bot-start flow (PR 2) and Telegram-deeplink unlock page (PR 3)
 * will also mount it.
 */
export function UnlockModal({ onUnlocked, onCancel }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const inputRef = useRef<HTMLInputElement>(null);

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
        if (res.ok) {
          // Clear before calling onUnlocked so any re-render that
          // unmounts us doesn't leak the value via React state.
          if (inputRef.current) inputRef.current.value = "";
          onUnlocked();
          return;
        }
        const data = await res.json().catch(() => null);
        setError(
          (data as { error?: string })?.error ?? `Unlock failed (${res.status})`,
        );
      } catch (err) {
        setError(
          err instanceof Error
            ? `Network error: ${err.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  // Open is always true; closing is driven by onUnlocked / onCancel
  // (which unmount us). When onCancel is omitted, we cancel any
  // close attempt at the source so the user can't dismiss without
  // unlocking — Base UI's `eventDetails.cancel()` rejects the close
  // (Escape, outside-press, programmatic) before it propagates.
  return (
    <Dialog.Root
      open
      onOpenChange={(open, eventDetails) => {
        if (open) return;
        if (!onCancel) {
          eventDetails.cancel();
          return;
        }
        onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-50 bg-background/80 backdrop-blur-sm" />
        <Dialog.Popup
          className="fixed left-1/2 top-1/2 z-50 w-full max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-lg border bg-background p-6 shadow-lg"
          initialFocus={inputRef}
        >
          <form onSubmit={submit}>
            <Dialog.Title className="text-lg font-semibold">
              Unlock credentials
            </Dialog.Title>
            <Dialog.Description className="mt-1 text-sm text-muted-foreground">
              Enter your passphrase to decrypt your stored credentials for
              this session.
            </Dialog.Description>
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
                <Dialog.Close
                  type="button"
                  disabled={isPending}
                  className="rounded border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
                >
                  Cancel
                </Dialog.Close>
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
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
