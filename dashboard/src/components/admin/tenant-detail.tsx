"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { LimitsForm } from "./limits-form";

type Bot = {
  id: string;
  mode: string;
  isRunning: boolean;
  containerName: string | null;
  lastStartedAt: string | null;
  lastStoppedAt: string | null;
};

type Trade = {
  id: number;
  strategyName: string;
  symbol: string;
  side: string;
  size: number;
  price: number;
  pnl: number | null;
  mode: string;
  timestamp: string;
};

type Detail = {
  tenant: {
    id: string;
    email: string;
    displayName: string | null;
    isOperator: boolean;
    isActive: boolean;
    createdAt: string;
    multiBotEnabled: boolean;
    limits: {
      maxActiveBots: number | null;
      maxActiveStrategies: number | null;
      allowedStrategies: string[] | null;
    };
  };
  bots: Bot[];
  pnl: Record<"7d" | "30d" | "all", { trades: number; pnl: number }>;
  recentTrades: Trade[];
};

function fmtMoney(n: number): string {
  const sign = n < 0 ? "-" : n > 0 ? "+" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

export function TenantDetail({ tenantId }: { tenantId: string }) {
  const [data, setData] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [meId, setMeId] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/tenant/me", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<{ id?: string }>) : null))
      .then((j) => setMeId(j?.id ?? null))
      .catch(() => undefined);
  }, []);

  const reload = () =>
    fetch(`/api/admin/tenants/${tenantId}`, { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<Detail>;
      })
      .then(setData)
      .catch((e) => setError(String(e)));

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  if (error) return <div className="text-sm text-destructive">Failed: {error}</div>;
  if (!data) return <div className="text-sm text-muted-foreground">Loading…</div>;

  const t = data.tenant;
  return (
    <>
      <header>
        <Link href="/admin" className="text-xs text-muted-foreground hover:underline">
          ← Admin
        </Link>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">
          {t.displayName || t.email}
        </h1>
        <p className="text-sm text-muted-foreground">{t.email}</p>
      </header>

      <section className="grid gap-3 md:grid-cols-3">
        {(["7d", "30d", "all"] as const).map((k) => (
          <div key={k} className="rounded border border-border p-3">
            <div className="text-xs uppercase text-muted-foreground">P&amp;L {k}</div>
            <div
              className={`mt-1 text-lg font-semibold ${
                data.pnl[k].pnl > 0
                  ? "text-emerald-500"
                  : data.pnl[k].pnl < 0
                    ? "text-destructive"
                    : ""
              }`}
            >
              {fmtMoney(data.pnl[k].pnl)}
            </div>
            <div className="text-xs text-muted-foreground">
              {data.pnl[k].trades} trades
            </div>
          </div>
        ))}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-muted-foreground">
          Bots
        </h2>
        <div className="grid gap-2 md:grid-cols-3">
          {data.bots.length === 0 && (
            <div className="text-sm text-muted-foreground">No bots.</div>
          )}
          {data.bots.map((b) => (
            <div key={b.id} className="rounded border border-border p-3 text-sm">
              <div className="font-medium">{b.mode}</div>
              <div
                className={`text-xs ${
                  b.isRunning ? "text-emerald-500" : "text-muted-foreground"
                }`}
              >
                {b.isRunning ? "running" : "stopped"}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                {b.containerName || "no container"}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-muted-foreground">
          Limits & allowlist
        </h2>
        <LimitsForm
          tenantId={tenantId}
          initial={t.limits}
          onSaved={reload}
          isSelf={meId === tenantId}
        />
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-muted-foreground">
          Recent trades
        </h2>
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full text-xs">
            <thead className="bg-muted/30 text-left uppercase text-muted-foreground">
              <tr>
                <th className="px-2 py-1">Time</th>
                <th className="px-2 py-1">Mode</th>
                <th className="px-2 py-1">Strategy</th>
                <th className="px-2 py-1">Symbol</th>
                <th className="px-2 py-1">Side</th>
                <th className="px-2 py-1">Size</th>
                <th className="px-2 py-1">Price</th>
                <th className="px-2 py-1">P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {data.recentTrades.map((tr) => (
                <tr key={tr.id} className="border-t border-border/60">
                  <td className="px-2 py-1">
                    {new Date(tr.timestamp).toLocaleString()}
                  </td>
                  <td className="px-2 py-1">{tr.mode}</td>
                  <td className="px-2 py-1">{tr.strategyName}</td>
                  <td className="px-2 py-1">{tr.symbol}</td>
                  <td className="px-2 py-1">{tr.side}</td>
                  <td className="px-2 py-1">{tr.size}</td>
                  <td className="px-2 py-1">{tr.price}</td>
                  <td
                    className={`px-2 py-1 ${
                      (tr.pnl ?? 0) > 0
                        ? "text-emerald-500"
                        : (tr.pnl ?? 0) < 0
                          ? "text-destructive"
                          : ""
                    }`}
                  >
                    {tr.pnl !== null ? fmtMoney(tr.pnl) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
