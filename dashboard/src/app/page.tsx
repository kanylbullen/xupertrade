import { OverviewView, type OverviewMode } from "./overview/_overview-view";

export const dynamic = "force-dynamic";

/**
 * Legacy `/?mode=...` overview entrypoint. Kept working unchanged
 * through PR A/B of the sidebar nav refactor so existing bookmarks and
 * the still-rendered top-bar Nav continue to function. PR C will swap
 * this for a 308 redirect to `/overview/<mode>`.
 */
export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const params = await searchParams;
  const rawMode = params.mode ?? "paper";
  const mode: OverviewMode =
    rawMode === "testnet" || rawMode === "mainnet" ? rawMode : "paper";

  return <OverviewView mode={mode} />;
}
