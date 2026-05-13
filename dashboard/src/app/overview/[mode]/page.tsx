import { notFound } from "next/navigation";
import { OverviewView, type OverviewMode } from "../_overview-view";

export const dynamic = "force-dynamic";

const VALID_MODES: ReadonlySet<OverviewMode> = new Set([
  "paper",
  "testnet",
  "mainnet",
]);

/**
 * Route-bound overview. PR A of the sidebar nav refactor: renders the
 * same body as `app/page.tsx` but sources `mode` from the route param,
 * so deep links like `/overview/testnet` no longer mutate global state
 * via `?mode=`. 404s on anything outside {paper, testnet, mainnet}.
 */
export default async function OverviewModePage({
  params,
}: {
  params: Promise<{ mode: string }>;
}) {
  const { mode } = await params;
  if (!VALID_MODES.has(mode as OverviewMode)) {
    notFound();
  }
  return <OverviewView mode={mode as OverviewMode} />;
}
