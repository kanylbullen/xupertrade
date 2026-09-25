import { tenantBotFetch } from "@/lib/bot-api";
import { parseBuildInfo } from "@/lib/build-info";

export const dynamic = "force-dynamic";

/**
 * Which build the calling tenant's bot runs (`?mode=`), for the bot
 * cards on /settings/bots. Behind the session like every tenant proxy;
 * the bot's own `/api/version` is public but only reachable on the
 * compose network. Errors pass through unchanged — a bot still on an
 * image from before the stamp answers 404. A 2xx is validated again,
 * so the page gets a hex SHA or "unknown", never arbitrary text.
 */
export async function GET(req: Request) {
  const res = await tenantBotFetch(req, "/api/version");
  if (!res.ok) return res;
  return Response.json(parseBuildInfo(await res.json().catch(() => null)));
}
