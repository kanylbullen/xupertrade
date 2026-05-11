import { botFetch } from "@/lib/bot-api";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  // Operator-only: TLS config is host-level (single Caddy instance
  // serving the LAN). A regular tenant must not be able to flip
  // domain / Cloudflare token / cert mode for the whole deploy.
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }
  const body = await req.json().catch(() => ({}));
  return botFetch(req, "/api/tls/configure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
