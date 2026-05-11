import { botFetch } from "@/lib/bot-api";
import { invalidateAuthCache } from "@/lib/auth";
import { requireOperator } from "@/lib/operator";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  // Operator-only: auth mode + OIDC config are host-level concerns
  // (single Authentik provider serving all tenants). Without this
  // gate any signed-in tenant could repoint OIDC to their own
  // Authentik instance and intercept future logins.
  //
  // Closes the isolation gap left over from Phase 6c PR ε (which
  // converted /api/control/* routes to tenantBotFetch but left this
  // one on botFetch with no auth check).
  try {
    await requireOperator(req);
  } catch (e) {
    if (e instanceof Response) return e;
    throw e;
  }
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
