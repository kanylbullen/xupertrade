import { botFetch } from "@/lib/bot-api";
import { invalidateAuthCache } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = await req.json().catch(() => ({}));
  const res = await botFetch(req, "/api/auth/configure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // Bust the in-process cache so the next page load sees the new mode
  invalidateAuthCache();
  return res;
}
