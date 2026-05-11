import { tenantBotFetch } from "@/lib/bot-api";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  return tenantBotFetch(req, "/api/control/pause", { method: "POST" });
}
