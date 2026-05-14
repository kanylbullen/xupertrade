import { notFound } from "next/navigation";

import { requireTenantServer } from "@/lib/tenant-server";

export const dynamic = "force-dynamic";

/**
 * Operator-only gate for /admin/*. Returns notFound() for non-operators
 * rather than 403 — `/admin` should not even exist from a regular
 * tenant's perspective. Distinct from the API gate (requireOperator)
 * which returns a structured 403 because API clients need to
 * distinguish the two cases.
 */
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const tenant = await requireTenantServer();
  if (tenant.isOperator !== true) notFound();
  return <>{children}</>;
}
