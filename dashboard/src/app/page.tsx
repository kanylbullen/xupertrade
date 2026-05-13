import { permanentRedirect } from "next/navigation";

export const dynamic = "force-dynamic";

const VALID_MODES = new Set(["paper", "testnet", "mainnet"]);

/**
 * Legacy `/?mode=...` entrypoint. After the sidebar cutover the
 * Overview lives at `/overview/<mode>` (route-bound). This page is now
 * a 308 redirect that preserves the legacy `?mode=` value so existing
 * bookmarks and any hand-typed URLs survive transparently.
 *
 * `?mode=` missing or unknown → redirect to `/overview/paper` (the
 * default mode the dashboard's always opened on).
 */
export default async function RootIndex({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}): Promise<never> {
  const params = await searchParams;
  const raw = params.mode;
  const mode = raw && VALID_MODES.has(raw) ? raw : "paper";
  // permanentRedirect issues a 308 (preserves method + body, signals
  // bookmark-update intent). The legacy mode value is preserved by
  // routing into the same mode at the new URL shape.
  permanentRedirect(`/overview/${mode}`);
}
