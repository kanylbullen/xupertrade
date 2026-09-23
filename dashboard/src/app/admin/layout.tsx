import { requireOperatorServer } from "@/lib/tenant-server";

export const dynamic = "force-dynamic";

/**
 * Operator-only gate for /admin/*. Returns notFound() for non-operators
 * rather than 403 — `/admin` should not even exist from a regular
 * tenant's perspective. Distinct from the API gate (requireOperator)
 * which returns a structured 403 because API clients need to
 * distinguish the two cases.
 *
 * Kept as the outer gate, but no longer the only one: the pages
 * beneath render concurrently with this layout, so each one calls
 * `requireOperatorServer` for itself too.
 */
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await requireOperatorServer();
  return <>{children}</>;
}
