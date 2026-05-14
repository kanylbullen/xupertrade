"use client";

import { useState } from "react";

import { KNOWN_STRATEGIES } from "@/lib/admin/strategy-names";

type Limits = {
  maxActiveBots: number | null;
  maxActiveStrategies: number | null;
  allowedStrategies: string[] | null;
};

type Warning =
  | { kind: "bots_over_cap"; current: number; limit: number }
  | { kind: "strategies_over_cap"; current: number; limit: number }
  | { kind: "active_strategies_outside_allowlist"; names: string[] };

function parseIntOrNull(s: string): number | null {
  if (s.trim() === "") return null;
  const n = Number(s);
  return Number.isInteger(n) && n >= 0 ? n : NaN as unknown as number;
}

export function LimitsForm({
  tenantId,
  initial,
  onSaved,
  isSelf,
}: {
  tenantId: string;
  initial: Limits;
  onSaved?: () => void;
  isSelf?: boolean;
}) {
  const [bots, setBots] = useState<string>(
    initial.maxActiveBots === null ? "" : String(initial.maxActiveBots),
  );
  const [strats, setStrats] = useState<string>(
    initial.maxActiveStrategies === null ? "" : String(initial.maxActiveStrategies),
  );
  const [allowAll, setAllowAll] = useState<boolean>(initial.allowedStrategies === null);
  const [allowed, setAllowed] = useState<Set<string>>(
    new Set(initial.allowedStrategies ?? []),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<Warning[]>([]);

  const toggle = (name: string) => {
    const next = new Set(allowed);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    setAllowed(next);
  };

  async function save() {
    setSaving(true);
    setError(null);
    setWarnings([]);
    const mb = parseIntOrNull(bots);
    const ms = parseIntOrNull(strats);
    if (Number.isNaN(mb) || Number.isNaN(ms)) {
      setError("Limits must be non-negative integers or empty for unlimited.");
      setSaving(false);
      return;
    }
    const body = {
      maxActiveBots: mb,
      maxActiveStrategies: ms,
      allowedStrategies: allowAll ? null : Array.from(allowed),
    };
    const r = await fetch(`/api/admin/tenants/${tenantId}/limits`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    setSaving(false);
    if (!r.ok) {
      const j = (await r.json().catch(() => ({}))) as { error?: string };
      setError(j.error || `HTTP ${r.status}`);
      return;
    }
    const j = (await r.json()) as { warnings: Warning[] };
    setWarnings(j.warnings);
    onSaved?.();
  }

  return (
    <div className="space-y-3 rounded border border-border p-3">
      {isSelf && (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-600">
          You are editing your own tenant. Lowering caps below current usage
          will block you from starting more bots/strategies.
        </div>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        <label className="block text-sm">
          <span className="text-muted-foreground">Max active bots (blank = unlimited, 0..10)</span>
          <input
            type="number"
            min={0}
            max={10}
            value={bots}
            onChange={(e) => setBots(e.target.value)}
            className="mt-1 w-full rounded border border-input bg-background px-2 py-1"
          />
        </label>
        <label className="block text-sm">
          <span className="text-muted-foreground">Max active strategies (blank = unlimited, 0..30)</span>
          <input
            type="number"
            min={0}
            max={30}
            value={strats}
            onChange={(e) => setStrats(e.target.value)}
            className="mt-1 w-full rounded border border-input bg-background px-2 py-1"
          />
        </label>
      </div>

      <div className="space-y-1">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={allowAll}
            onChange={(e) => setAllowAll(e.target.checked)}
          />
          <span>Allow all strategies</span>
        </label>
        {!allowAll && (
          <div className="grid grid-cols-2 gap-1 md:grid-cols-3 lg:grid-cols-4">
            {KNOWN_STRATEGIES.map((n) => (
              <label key={n} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={allowed.has(n)}
                  onChange={() => toggle(n)}
                />
                <span>{n}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      {error && <div className="text-sm text-destructive">{error}</div>}
      {warnings.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-amber-500">
          {warnings.map((w, i) => (
            <li key={i}>
              {w.kind === "bots_over_cap" &&
                `Bots over cap: ${w.current} running, limit ${w.limit}`}
              {w.kind === "strategies_over_cap" &&
                `Strategies over cap: ${w.current} active, limit ${w.limit}`}
              {w.kind === "active_strategies_outside_allowlist" &&
                `Active strategies outside allowlist: ${w.names.join(", ")}`}
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        onClick={save}
        disabled={saving}
        className="rounded bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {saving ? "Saving…" : "Save limits"}
      </button>
    </div>
  );
}
