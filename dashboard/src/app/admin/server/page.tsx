import Link from "next/link";

import { ServerStatsCard } from "@/components/admin/server-stats-card";

export const dynamic = "force-dynamic";

export default function AdminServerPage() {
  return (
    <main className="space-y-4">
      <header>
        <Link href="/admin" className="text-xs text-muted-foreground hover:underline">
          ← Admin
        </Link>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">Server stats</h1>
        <p className="text-sm text-muted-foreground">
          Live snapshot of the host's CPU, memory, disk, and Docker.
          Polled every 5 seconds.
        </p>
      </header>
      <ServerStatsCard />
    </main>
  );
}
