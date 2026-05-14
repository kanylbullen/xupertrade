import { TenantDetail } from "@/components/admin/tenant-detail";

export const dynamic = "force-dynamic";

type Params = { params: Promise<{ tenantId: string }> };

export default async function AdminTenantPage({ params }: Params) {
  const { tenantId } = await params;
  return (
    <main className="space-y-6">
      <TenantDetail tenantId={tenantId} />
    </main>
  );
}
