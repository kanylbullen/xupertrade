"use client";

import Link from "next/link";
import { useEffect, useState, useTransition } from "react";

import { UnlockModal } from "@/components/unlock-modal";

const MODES = ["paper", "testnet", "mainnet"] as const;
type Mode = (typeof MODES)[number];

type BotRow = {
  id: string;
  mode: Mode;
  containerId: string | null;
  containerName: string | null;
  isRunning: boolean;
  createdAt: string;
  lastStartedAt: string | null;
  lastStoppedAt: string | null;
};

export function BotsClient() {
  const [bots, setBots] = useState<BotRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showUnlock, setShowUnlock] = useState(false);

  const refresh = async () => {
    try {
      const r = await fetch("/api/tenant/me/bots", { cache: "no-store" });
      if (!r.ok) {
        setError(`failed to load bots (${r.status})`);
        return;
      }
      const data = (await r.json()) as { bots: BotRow[] };
      setBots(data.bots);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  if (error) {
    return (
      <div className="rounded border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-500">
        {error}
      </div>
    );
  }
  if (bots === null) {
    return <div className="text-sm text-muted-foreground">Loading…</div>;
  }

  const byMode = new Map(bots.map((b) => [b.mode, b]));

  return (
    <div className="space-y-4">
      {MODES.map((mode) => (
        <BotCard
          key={mode}
          mode={mode}
          bot={byMode.get(mode) ?? null}
          onChange={refresh}
          onLocked={() => setShowUnlock(true)}
        />
      ))}
      {showUnlock && (
        <UnlockModal
          onUnlocked={() => {
            setShowUnlock(false);
            refresh();
          }}
          onCancel={() => setShowUnlock(false)}
        />
      )}
      <SendUnlockLinkButton />
      <p className="text-xs text-muted-foreground">
        Need to set credentials first?{" "}
        <Link
          href="/settings/credentials"
          className="underline hover:text-foreground"
        >
          Go to credentials
        </Link>
      </p>
    </div>
  );
}

/**
 * Triggers the bot to DM the tenant a signed unlock-deeplink.
 * Useful when the tenant left the dashboard, K-cache expired, but
 * they want to start their bot from mobile via the Telegram-link.
 * Requires Telegram to be linked AND at least one bot running.
 */
function SendUnlockLinkButton() {
  const [pending, setPending] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );

  async function send() {
    setPending(true);
    setMsg(null);
    try {
      const res = await fetch("/api/tenant/me/telegram/send-unlock-link", {
        method: "POST",
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: "Unlock link sent — check your Telegram." });
      } else {
        const data = await res.json().catch(() => null);
        setMsg({
          kind: "err",
          text:
            (data as { error?: string })?.error ?? `Failed (${res.status})`,
        });
      }
    } catch (e) {
      setMsg({
        kind: "err",
        text:
          e instanceof Error
            ? `Network error: ${e.message}`
            : "Network error — check your connection and try again.",
      });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="rounded-lg border p-4">
      <h3 className="text-sm font-medium">Trouble unlocking?</h3>
      <p className="mt-1 text-xs text-muted-foreground">
        Send yourself a Telegram deeplink that lets you unlock from any
        device without re-opening the dashboard. Requires Telegram to be
        linked under{" "}
        <Link
          href="/settings/credentials"
          className="underline hover:text-foreground"
        >
          Credentials
        </Link>{" "}
        and at least one running bot.
      </p>
      <button
        type="button"
        onClick={send}
        disabled={pending}
        className="mt-3 rounded border px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
      >
        {pending ? "Sending…" : "Send unlock link to Telegram"}
      </button>
      {msg && (
        <p
          className={`mt-2 text-xs ${
            msg.kind === "ok" ? "text-green-500" : "text-red-500"
          }`}
          role={msg.kind === "err" ? "alert" : undefined}
        >
          {msg.text}
        </p>
      )}
    </div>
  );
}

function BotCard({
  mode,
  bot,
  onChange,
  onLocked,
}: {
  mode: Mode;
  bot: BotRow | null;
  onChange: () => void;
  onLocked: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  function call(method: "POST" | "DELETE", url: string) {
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch(url, { method });
        if (res.status === 401) {
          onLocked();
          return;
        }
        if (!res.ok) {
          const data = await res.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ?? `Failed (${res.status})`,
          );
          return;
        }
        onChange();
      } catch (err) {
        setError(
          err instanceof Error
            ? `Network error: ${err.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  function create() {
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch("/api/tenant/me/bots", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ mode }),
        });
        if (res.status === 401) {
          onLocked();
          return;
        }
        if (!res.ok) {
          const data = await res.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ?? `Failed (${res.status})`,
          );
          return;
        }
        onChange();
      } catch (err) {
        setError(
          err instanceof Error
            ? `Network error: ${err.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  const status = !bot
    ? { label: "Not created", color: "muted" as const }
    : bot.isRunning
      ? { label: "Running", color: "green" as const }
      : { label: "Stopped", color: "amber" as const };

  return (
    <div className="rounded-lg border p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium uppercase tracking-wide">
              {mode}
            </h3>
            <span
              className={
                status.color === "green"
                  ? "rounded-full bg-green-500/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-green-500"
                  : status.color === "amber"
                    ? "rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-500"
                    : "rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
              }
            >
              {status.label}
            </span>
          </div>
          {bot && (
            <div className="mt-1 space-y-0.5 text-[11px] text-muted-foreground">
              {bot.containerName && (
                <div className="font-mono truncate">{bot.containerName}</div>
              )}
              {bot.lastStartedAt && (
                <div>
                  Started {new Date(bot.lastStartedAt).toLocaleString()}
                </div>
              )}
              {bot.lastStoppedAt && !bot.isRunning && (
                <div>
                  Stopped {new Date(bot.lastStoppedAt).toLocaleString()}
                </div>
              )}
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          {!bot && (
            <button
              type="button"
              onClick={create}
              disabled={isPending}
              className="rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isPending ? "Starting…" : "Create + start"}
            </button>
          )}
          {bot && !bot.isRunning && (
            <button
              type="button"
              onClick={() =>
                call("POST", `/api/tenant/me/bots/${bot.id}/start`)
              }
              disabled={isPending}
              className="rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isPending ? "Starting…" : "Start"}
            </button>
          )}
          {bot && bot.isRunning && (
            <button
              type="button"
              onClick={() =>
                call("POST", `/api/tenant/me/bots/${bot.id}/stop`)
              }
              disabled={isPending}
              className="rounded border px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
            >
              {isPending ? "Stopping…" : "Stop"}
            </button>
          )}
          {bot && (
            <button
              type="button"
              onClick={() => {
                if (
                  !confirm(
                    `Delete ${mode} bot? This stops the container and removes the row.`,
                  )
                )
                  return;
                call("DELETE", `/api/tenant/me/bots/${bot.id}`);
              }}
              disabled={isPending}
              className="rounded border border-red-500/40 px-3 py-1.5 text-xs text-red-500 hover:bg-red-500/10 disabled:opacity-50"
            >
              Delete
            </button>
          )}
        </div>
      </div>
      {error && (
        <p className="mt-3 text-sm text-red-500" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
