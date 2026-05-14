"use client";

import { useEffect, useState } from "react";

type Stats = {
  cpu: { loadAvg: [number, number, number]; cores: number; usagePct: number };
  memory: { totalMB: number; usedMB: number; freeMB: number; cachedMB: number };
  disk: { mount: string; totalGB: number; usedGB: number; freeGB: number; usePct: number }[];
  docker: { running: number; total: number };
};

export function ServerStatsCard() {
  const [s, setS] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const tick = () =>
      fetch("/api/admin/server-stats", { cache: "no-store" })
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json() as Promise<Stats>;
        })
        .then((j) => active && setS(j))
        .catch((e) => active && setError(String(e)));
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  if (error) return <div className="text-sm text-destructive">Failed: {error}</div>;
  if (!s) return <div className="text-sm text-muted-foreground">Loading…</div>;

  return (
    <div className="grid gap-3 md:grid-cols-2">
      <div className="rounded border border-border p-3">
        <div className="text-xs uppercase text-muted-foreground">CPU</div>
        <div className="mt-1 text-2xl font-semibold">{s.cpu.usagePct.toFixed(1)}%</div>
        <div className="text-xs text-muted-foreground">
          {s.cpu.cores} cores · load {s.cpu.loadAvg.map((n) => n.toFixed(2)).join(" / ")}
        </div>
      </div>
      <div className="rounded border border-border p-3">
        <div className="text-xs uppercase text-muted-foreground">Memory</div>
        <div className="mt-1 text-2xl font-semibold">
          {s.memory.usedMB.toLocaleString()} / {s.memory.totalMB.toLocaleString()} MB
        </div>
        <div className="text-xs text-muted-foreground">
          {s.memory.cachedMB.toLocaleString()} MB cached · {s.memory.freeMB.toLocaleString()} MB free
        </div>
      </div>
      {s.disk.map((d) => (
        <div key={d.mount} className="rounded border border-border p-3">
          <div className="text-xs uppercase text-muted-foreground">Disk {d.mount}</div>
          <div className="mt-1 text-2xl font-semibold">{d.usePct}%</div>
          <div className="text-xs text-muted-foreground">
            {d.usedGB} / {d.totalGB} GB · {d.freeGB} GB free
          </div>
        </div>
      ))}
      <div className="rounded border border-border p-3">
        <div className="text-xs uppercase text-muted-foreground">Docker</div>
        <div className="mt-1 text-2xl font-semibold">
          {s.docker.running} / {s.docker.total}
        </div>
        <div className="text-xs text-muted-foreground">running / total containers</div>
      </div>
    </div>
  );
}
