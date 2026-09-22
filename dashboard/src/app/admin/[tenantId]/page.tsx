import { TenantDetail } from "@/components/admin/tenant-detail";
import { requireOperatorServer } from "@/lib/tenant-server";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ tenantId: string }> };

export default async function AdminTenantPage({ params }: Params) {
  // Self-gating: `admin/layout.tsx` renders concurrently with this
  // page, so its notFound() doesn't prevent this body from running.
  // This page takes a tenant id from the URL, so it is the one that
  // would most obviously grow a server-side read of another tenant.
  await requireOperatorServer();
  const { tenantId } = await params;
  return (
    <main className="space-y-6">
      <TenantDetail tenantId={tenantId} />
    </main>
  );
}
