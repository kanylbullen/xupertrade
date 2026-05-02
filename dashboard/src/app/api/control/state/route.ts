import { botFetch } from "@/lib/bot-api";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  return botFetch(req, "/api/control/state");
}
