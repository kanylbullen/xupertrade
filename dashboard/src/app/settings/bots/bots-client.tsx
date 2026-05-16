"use client";

import Link from "next/link";
import { useEffect, useState, useTransition } from "react";

import { UnlockModal } from "@/components/unlock-modal";
import { LiveLog } from "@/components/live-log";
import { MainnetStrategiesCard } from "@/components/mainnet-strategies-card";

const MODES = ["mainnet", "testnet", "paper"] as const;
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
      <MainnetStrategiesCard />
      {/* Tenant-wide live event stream — was previously the LiveLog
          on the retired /status page (Decision 3 of the sidebar nav
          refactor). One panel because the underlying Redis pub/sub
          is tenant-wide, not per-bot. Collapsed by default to keep
          the page calm; expanding it kicks off the SSE connection. */}
      <RecentEventsPanel />
    </div>
  );
}

function RecentEventsPanel() {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-lg border bg-card"
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-medium">
        Recent events {open ? "▾" : "▸"}
      </summary>
      <div className="border-t p-4">
        {open ? (
          <LiveLog />
        ) : (
          <p className="text-xs text-muted-foreground">
            Expand to start streaming live trade / heartbeat / signal events.
          </p>
        )}
      </div>
    </details>
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
  const [reconcileMsg, setReconcileMsg] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  function reconcile() {
    if (!bot) return;
    setError(null);
    setReconcileMsg(null);
    startTransition(async () => {
      try {
        const res = await fetch(
          `/api/tenant/me/bots/${bot.id}/reconcile`,
          { method: "POST" },
        );
        if (res.status === 401) {
          onLocked();
          return;
        }
        const data = (await res.json().catch(() => null)) as
          | { examined?: number; inserted?: number; skipped?: number; error?: string }
          | null;
        if (!res.ok) {
          setError(data?.error ?? `Failed (${res.status})`);
          return;
        }
        setReconcileMsg(
          `Reconciled: ${data?.inserted ?? 0} inserted, ${data?.skipped ?? 0} skipped (examined ${data?.examined ?? 0})`,
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
              onClick={reconcile}
              disabled={isPending}
              title="Backfill missing trade rows from HyperLiquid fill history"
              className="rounded border px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
            >
              {isPending ? "Working…" : "Reconcile fills"}
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
      {reconcileMsg && (
        <p className="mt-3 text-sm text-muted-foreground" role="status">
          {reconcileMsg}
        </p>
      )}
      {bot && bot.isRunning && (
        <BotRuntime mode={mode} startedAt={bot.lastStartedAt} />
      )}
    </div>
  );
}

type RuntimeState = {
  paused: boolean;
  disabled_strategies: string[];
  open_positions: number;
  equity: number;
};

/**
 * Per-bot runtime stats — heartbeat / paused / open positions /
 * equity. Replaces what used to live on the retired `/status` page
 * (Decision 3 of the sidebar nav refactor).
 *
 * Source: `/api/control/state?mode=<mode>` proxies to the running
 * bot. Heartbeat age and last-trade-time would need new bot-side
 * endpoints to surface verbatim — TODO below; the dashboard renders
 * what it can today and adds them once exposed.
 */
/**
 * How long after `startedAt` we treat 5xx / network errors as "still
 * booting" rather than a hard failure. Bot startup includes HL SDK
 * init (meta + spot_meta fetches with retry, can take 5-30s on a
 * good day, 60s+ during HL hiccups), aiohttp listen-bind, and Redis/
 * Postgres connection pool warm-up. After the grace window we revert
 * to the verbose "HTTP 502" message so a chronically-failing bot
 * gets surfaced instead of stuck on "Starting…" forever.
 */
const STARTUP_GRACE_MS = 90_000;

function BotRuntime({
  mode,
  startedAt,
}: {
  mode: Mode;
  startedAt: string | null;
}) {
  const [state, setState] = useState<RuntimeState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetchAt, setLastFetchAt] = useState<Date | null>(null);
  // Re-render every 5s so the grace window flips to the verbose
  // error message at the right time without waiting for a fetch.
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 5_000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch(
          `/api/control/state?mode=${mode}`,
          { cache: "no-store" },
        );
        if (!res.ok) {
          if (!cancelled) {
            setError(`HTTP ${res.status}`);
            setState(null);
          }
          return;
        }
        const data = (await res.json()) as RuntimeState;
        if (!cancelled) {
          setState(data);
          setError(null);
          setLastFetchAt(new Date());
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "fetch failed");
          setState(null);
        }
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [mode]);

  if (error) {
    // First N seconds after a Start click, the bot's API isn't bound
    // yet (Python startup + HL SDK init). 502 from the dashboard
    // proxy is normal here — render "Starting…" so the operator
    // doesn't think something's broken on their first deploy.
    // Outside the grace window, surface the verbose error so a
    // chronically-failing bot is visible rather than masked.
    const startedAtMs = startedAt ? new Date(startedAt).getTime() : 0;
    const inGrace =
      startedAtMs > 0 && Date.now() - startedAtMs < STARTUP_GRACE_MS;
    return (
      <div className="mt-3 border-t pt-3 text-xs text-muted-foreground">
        {inGrace ? (
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-yellow-400" />
            Starting…
          </span>
        ) : (
          <>Runtime status unavailable: {error}</>
        )}
      </div>
    );
  }
  if (!state) {
    return (
      <div className="mt-3 border-t pt-3 text-xs text-muted-foreground">
        Loading runtime status…
      </div>
    );
  }

  return (
    <div className="mt-3 grid gap-2 border-t pt-3 text-xs sm:grid-cols-4">
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Trading
        </div>
        <div className={state.paused ? "text-yellow-400" : "text-green-400"}>
          {state.paused ? "Paused" : "Running"}
        </div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Open positions
        </div>
        <div className="font-mono">{state.open_positions}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Equity
        </div>
        <div className="font-mono">${state.equity.toLocaleString()}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Disabled strategies
        </div>
        <div className="font-mono">{state.disabled_strategies.length}</div>
      </div>
      {state.disabled_strategies.length > 0 && (
        <div className="sm:col-span-4 text-[11px] text-muted-foreground">
          Off: {state.disabled_strategies.join(", ")}
        </div>
      )}
      {lastFetchAt && (
        <div className="sm:col-span-4 text-[10px] text-muted-foreground">
          Last polled {lastFetchAt.toLocaleTimeString("sv-SE", { hour12: false })}
        </div>
      )}
      {/* TODO: surface heartbeat age + last-trade time + per-bot
          restart action. Needs new bot-side endpoints (`/api/control/heartbeat`
          read, `/api/last-trade`) wired through tenantBotFetch with
          explicit `?mode=<mode>` propagation. */}
    </div>
  );
}
