import { getCaddyStatus } from "@/lib/caddy-admin";
import { requireOperator } from "@/lib/operator";
import { getTlsConfig } from "@/lib/tls-config";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  // Operator-only: leaks the configured domain/email otherwise.
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }

  const cfg = await getTlsConfig();
  const caddyStatus = await getCaddyStatus();

  // Same response shape as the bot's tls_get_config — never
  // returns the Cloudflare API token, just whether it's set.
  return Response.json({
    enabled: cfg.enabled,
    domain: cfg.domain,
    email: cfg.email,
    cf_token_set: Boolean(cfg.cf_token),
    caddy_status: caddyStatus,
  });
}
