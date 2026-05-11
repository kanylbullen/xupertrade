import { botFetch } from "@/lib/bot-api";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  // Operator-only: bot's tls_get_config is auth-gated and the dashboard
  // proxy forwards API_KEY. Returning the proxied payload to a
  // non-operator tenant would leak the configured domain/email.
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }
  return botFetch(req, "/api/tls/config");
}
