import Link from "next/link";

import { TenantTable } from "@/components/admin/tenant-table";
import { requireOperatorServer } from "@/lib/tenant-server";

export const dynamic = "force-dynamic";

export default async function AdminOverviewPage() {
  // Self-gating: `admin/layout.tsx` renders concurrently with this
  // page, so its notFound() doesn't prevent this body from running.
  await requireOperatorServer();
  return (
    <main className="space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Admin</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Operator-only overview of every tenant on this host.
          </p>
        </div>
        <nav className="flex gap-3 text-sm">
          <Link href="/admin/server" className="text-foreground hover:underline">
            Server stats
          </Link>
        </nav>
      </header>
      <TenantTable />
    </main>
  );
}
