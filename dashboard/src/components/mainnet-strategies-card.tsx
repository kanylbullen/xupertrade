"use client";

import { useEffect, useState, useTransition } from "react";

import { Switch } from "@/components/ui/switch";

type StrategyMeta = {
  name: string;
  symbol: string;
  timeframe: string;
  summary: string;
};

type Data = {
  operator_cap: string[];
  tenant_enabled: string[];
  all_strategies: StrategyMeta[];
};

type Status = "operator-disabled" | "available" | "enabled";

function statusFor(
  name: string,
  cap: Set<string>,
  enabled: Set<string>,
): Status {
  if (!cap.has(name)) return "operator-disabled";
  return enabled.has(name) ? "enabled" : "available";
}

export function MainnetStrategiesCard() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingNames, setPendingNames] = useState<Set<string>>(new Set());
  const [, startTransition] = useTransition();

  async function refresh() {
    try {
      const r = await fetch("/api/tenant/me/mainnet-strategies", {
        cache: "no-store",
      });
      if (!r.ok) {
        setError(`failed to load (${r.status})`);
        return;
      }
      setData((await r.json()) as Data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  function toggle(name: string, next: boolean) {
    setPendingNames((prev) => new Set(prev).add(name));
    startTransition(async () => {
      try {
        const r = await fetch(
          `/api/tenant/me/mainnet-strategies/${encodeURIComponent(name)}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: next }),
          },
        );
        if (!r.ok) {
          const body = (await r.json().catch(() => ({}))) as {
            error?: string;
          };
          setError(`toggle failed: ${body.error ?? r.status}`);
        }
      } finally {
        setPendingNames((prev) => {
          const c = new Set(prev);
          c.delete(name);
          return c;
        });
        await refresh();
      }
    });
  }

  return (
    <section className="rounded-lg border bg-card p-4">
      <header className="mb-3">
        <h2 className="text-base font-semibold">Mainnet strategies</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Only strategies in the operator-approved set can be enabled
          for mainnet trading. Tenant-side toggles here take effect
          immediately — the bot picks them up on its next tick (no
          restart needed).
        </p>
      </header>

      {error && (
        <div className="mb-3 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-500">
          {error}
        </div>
      )}

      {data === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : (
        <MainnetStrategiesList
          data={data}
          pendingNames={pendingNames}
          onToggle={toggle}
        />
      )}
    </section>
  );
}

export function MainnetStrategiesList({
  data,
  pendingNames,
  onToggle,
}: {
  data: Data;
  pendingNames: Set<string>;
  onToggle: (name: string, next: boolean) => void;
}) {
  const cap = new Set(data.operator_cap);
  const enabled = new Set(data.tenant_enabled);

  if (cap.size === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        The operator has not approved any strategies for mainnet
        trading. Nothing can be enabled until they update the
        <code className="mx-1 rounded bg-muted px-1 font-mono text-xs">
          HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES
        </code>
        environment variable.
      </p>
    );
  }

  return (
    <ul className="divide-y divide-border">
      {data.all_strategies.map((s) => {
        const status = statusFor(s.name, cap, enabled);
        const isPending = pendingNames.has(s.name);
        const checked = status === "enabled";
        const switchDisabled = status === "operator-disabled" || isPending;
        return (
          <li key={s.name} className="flex items-start gap-3 py-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm font-semibold">
                  {s.name}
                </span>
                <span className="rounded border px-1 font-mono text-[10px] text-muted-foreground">
                  {s.symbol} {s.timeframe}
                </span>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                {s.summary}
              </p>
              <p className="mt-1 text-[11px]">
                <StatusLabel status={status} />
              </p>
            </div>
            <Switch
              checked={checked}
              disabled={switchDisabled}
              onCheckedChange={(next: boolean) => onToggle(s.name, next)}
              aria-label={`Mainnet trading for ${s.name}`}
            />
          </li>
        );
      })}
    </ul>
  );
}

function StatusLabel({ status }: { status: Status }) {
  if (status === "operator-disabled") {
    return (
      <span className="text-muted-foreground">Disabled by operator</span>
    );
  }
  if (status === "available") {
    return (
      <span className="text-amber-400">
        Available — enable to start trading on mainnet
      </span>
    );
  }
  return <span className="text-green-400">Enabled</span>;
}
