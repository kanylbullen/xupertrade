import { NextResponse } from "next/server";

import { getBuildInfo } from "@/lib/build-info";

// Must render per request: the build-info file only exists in the
// runner image, so a response prerendered during `next build` would
// bake in "unknown" for good.
export const dynamic = "force-dynamic";

/**
 * Which build this dashboard runs: `{ sha, built_at }`. Public (listed
 * in `proxy.ts`'s PUBLIC_PATHS) like `/api/healthz` — a commit hash of
 * a public repo and a build time are not secrets, and checking a
 * deploy should not need a session.
 */
export async function GET() {
  return NextResponse.json(getBuildInfo());
}
