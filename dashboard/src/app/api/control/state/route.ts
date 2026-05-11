import { tenantBotFetch } from "@/lib/bot-api";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  return tenantBotFetch(req, "/api/control/state");
}
