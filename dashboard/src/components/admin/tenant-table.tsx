"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

type TenantRow = {
  id: string;
  email: string;
  displayName: string | null;
  isOperator: boolean;
  isActive: boolean;
  createdAt: string;
  lastSeenAt: string | null;
  activeBotsCount: number;
  totalBotsCount: number;
  trades7d: number;
  pnl7d: number;
  limits: {
    maxActiveBots: number | null;
    maxActiveStrategies: number | null;
    allowedStrategies: string[] | null;
  };
};

function fmtMoney(n: number): string {
  const sign = n < 0 ? "-" : n > 0 ? "+" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

export function TenantTable() {
  const [rows, setRows] = useState<TenantRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [meId, setMeId] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/admin/tenants", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<{ tenants: TenantRow[] }>;
      })
      .then((j) => setRows(j.tenants))
      .catch((e) => setError(String(e)));
    fetch("/api/tenant/me", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<{ id?: string }>) : null))
      .then((j) => setMeId(j?.id ?? null))
      .catch(() => undefined);
  }, []);

  if (error)
    return (
      <div className="rounded border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
        Failed to load: {error}
      </div>
    );
  if (!rows) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="overflow-x-auto rounded border border-border">
      <table className="w-full text-sm">
        <thead className="bg-muted/30 text-left text-xs uppercase text-muted-foreground">
          <tr>
            <th className="px-3 py-2">Tenant</th>
            <th className="px-3 py-2">Bots</th>
            <th className="px-3 py-2">Trades 7d</th>
            <th className="px-3 py-2">P&amp;L 7d</th>
            <th className="px-3 py-2">Limits</th>
            <th className="px-3 py-2">Last seen</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => {
            const overCap =
              t.limits.maxActiveBots !== null &&
              t.activeBotsCount > t.limits.maxActiveBots;
            return (
              <tr key={t.id} className="border-t border-border/60">
                <td className="px-3 py-2">
                  <Link
                    href={`/admin/${t.id}`}
                    className="font-medium hover:underline"
                  >
                    {t.displayName || t.email}
                    {meId === t.id && (
                      <span className="ml-1 text-xs text-muted-foreground">(you)</span>
                    )}
                  </Link>
                  <div className="text-xs text-muted-foreground">{t.email}</div>
                  {t.isOperator && (
                    <span className="mt-1 inline-block rounded bg-primary/20 px-1.5 py-0.5 text-[10px] uppercase text-primary">
                      operator
                    </span>
                  )}
                  {!t.isActive && (
                    <span className="ml-1 mt-1 inline-block rounded bg-destructive/20 px-1.5 py-0.5 text-[10px] uppercase text-destructive">
                      disabled
                    </span>
                  )}
                </td>
                <td className={`px-3 py-2 ${overCap ? "text-destructive" : ""}`}>
                  {t.activeBotsCount}/{t.totalBotsCount}
                </td>
                <td className="px-3 py-2">{t.trades7d}</td>
                <td
                  className={`px-3 py-2 ${
                    t.pnl7d > 0 ? "text-emerald-500" : t.pnl7d < 0 ? "text-destructive" : ""
                  }`}
                >
                  {fmtMoney(t.pnl7d)}
                </td>
                <td className="px-3 py-2 text-xs">
                  bots: {t.limits.maxActiveBots ?? "—"} · strats:{" "}
                  {t.limits.maxActiveStrategies ?? "—"} · allow:{" "}
                  {t.limits.allowedStrategies === null
                    ? "all"
                    : `${t.limits.allowedStrategies.length}`}
                </td>
                <td className="px-3 py-2 text-xs text-muted-foreground">
                  {t.lastSeenAt
                    ? new Date(t.lastSeenAt).toLocaleString()
                    : "never"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
