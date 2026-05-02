import { botFetch } from "@/lib/bot-api";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  return botFetch(req, "/api/control/pause", { method: "POST" });
}
