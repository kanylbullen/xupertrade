"use client";

import { useCallback, useEffect, useState, useTransition } from "react";

type LinkStatus =
  | { linked: false }
  | {
      linked: true;
      chatId: string;
      username: string | null;
      linkedAt: string;
      lastUnlockAt: string | null;
    };

type CodeResponse = {
  code: string;
  expiresInSeconds: number;
};

/**
 * Telegram link card — lets the tenant generate a 6-digit code to
 * pair their Telegram chat with their account, view linked status,
 * or unlink.
 *
 * The pairing itself is completed by the user sending
 * `/link <code>` to the bot in Telegram (PR 3b).
 */
export function TelegramLinkCard() {
  const [status, setStatus] = useState<LinkStatus | null>(null);
  const [code, setCode] = useState<CodeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  // Wrapped in useCallback so the useEffect deps below are stable
  // and we don't re-create the closure on every render (which would
  // tear down the 10s polling interval each tick).
  const refresh = useCallback(async () => {
    try {
      const r = await fetch("/api/tenant/me/telegram/link", {
        cache: "no-store",
      });
      if (!r.ok) {
        setError(`failed to load status (${r.status})`);
        return;
      }
      setStatus((await r.json()) as LinkStatus);
      // Clear any prior error on a successful fetch — without this
      // a transient network blip would leave the error banner up
      // even after the next poll cleanly succeeded.
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!code) return;
    // Re-fetch every 10s while a code is showing so the UI flips
    // to "Linked" without manual refresh once the user sends /link
    // to the bot.
    const t = setInterval(refresh, 10_000);
    return () => clearInterval(t);
  }, [code, refresh]);

  // If we just minted a code but the tenant already linked,
  // dismiss the code box (would otherwise show stale).
  useEffect(() => {
    if (code && status?.linked === true) setCode(null);
  }, [status, code]);

  function generate() {
    setError(null);
    startTransition(async () => {
      try {
        const r = await fetch("/api/tenant/me/telegram/link", {
          method: "POST",
        });
        if (!r.ok) {
          const data = await r.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ?? `Failed (${r.status})`,
          );
          return;
        }
        setCode((await r.json()) as CodeResponse);
      } catch (e) {
        setError(
          e instanceof Error
            ? `Network error: ${e.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  function unlink() {
    if (!confirm("Unlink Telegram? You'll stop getting unlock notifications.")) {
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        const r = await fetch("/api/tenant/me/telegram/link", {
          method: "DELETE",
        });
        if (!r.ok) {
          const data = await r.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ?? `Failed (${r.status})`,
          );
          return;
        }
        setCode(null);
        refresh();
      } catch (e) {
        setError(
          e instanceof Error
            ? `Network error: ${e.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  return (
    <div className="rounded-lg border p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <h3 className="text-sm font-medium">Telegram</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Link your Telegram chat to get unlock notifications when your
            bot needs your passphrase after a restart.
          </p>
        </div>
        {status?.linked === true ? (
          <span className="rounded-full bg-green-500/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-green-500 shrink-0">
            Linked
          </span>
        ) : (
          <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground shrink-0">
            Not linked
          </span>
        )}
      </div>

      {error && (
        <p className="mt-3 text-sm text-red-500" role="alert">
          {error}
        </p>
      )}

      {status === null && (
        <p className="mt-3 text-sm text-muted-foreground">Loading…</p>
      )}

      {status?.linked === true && (
        <div className="mt-3 space-y-1 text-xs text-muted-foreground">
          {status.username && <p>Chat: @{status.username}</p>}
          <p>Linked {new Date(status.linkedAt).toLocaleString()}</p>
          {status.lastUnlockAt && (
            <p>Last unlock {new Date(status.lastUnlockAt).toLocaleString()}</p>
          )}
          <div className="mt-3">
            <button
              type="button"
              onClick={unlink}
              disabled={pending}
              className="rounded border border-red-500/40 px-3 py-1 text-xs text-red-500 hover:bg-red-500/10 disabled:opacity-50"
            >
              Unlink
            </button>
          </div>
        </div>
      )}

      {status?.linked === false && !code && (
        <div className="mt-3">
          <button
            type="button"
            onClick={generate}
            disabled={pending}
            className="rounded border px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
          >
            {pending ? "Generating…" : "Generate code"}
          </button>
        </div>
      )}

      {code && status?.linked === false && (
        <div className="mt-3 rounded border bg-muted/30 p-3">
          <p className="text-xs text-muted-foreground">
            Send this command to your Telegram bot:
          </p>
          <code className="mt-2 block break-all rounded bg-background px-2 py-1.5 text-sm font-mono">
            /link {code.code}
          </code>
          <p className="mt-2 text-[11px] text-muted-foreground">
            Code expires in {Math.floor(code.expiresInSeconds / 60)} min.
            The page will update once you send the command.
          </p>
        </div>
      )}
    </div>
  );
}
