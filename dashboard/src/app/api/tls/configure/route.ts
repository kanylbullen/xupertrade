import { pushTlsConfig } from "@/lib/caddy-admin";
import { requireOperator } from "@/lib/operator";
import {
  getTlsConfig,
  setTlsConfig,
  type TlsConfig,
} from "@/lib/tls-config";

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

  const raw = await req.json().catch(() => null);
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
    return Response.json({ error: "body must be a JSON object" }, { status: 400 });
  }
  const body = raw as Record<string, unknown>;

  const updates: Partial<TlsConfig> = {};
  if ("enabled" in body) {
    const v = body.enabled;
    // Strict: enabled must be a real boolean. Boolean(v) would
    // treat the string "false" or the number 0 as truthy, which
    // could silently flip TLS on. Match the strictness used for
    // the string-field validation below.
    if (typeof v !== "boolean") {
      return Response.json(
        { error: "enabled must be a boolean" },
        { status: 400 },
      );
    }
    updates.enabled = v;
  }
  for (const k of ["domain", "email", "cf_token"] as const) {
    if (k in body) {
      const v = body[k];
      if (typeof v !== "string") {
        return Response.json(
          { error: `${k} must be a string` },
          { status: 400 },
        );
      }
      updates[k] = v.trim();
    }
  }

  // Mirror bot's behavior: persist first, then read back to pass
  // a canonical view to Caddy. This way a partial update (e.g.
  // just toggling enabled) still composes with previously-saved
  // domain/email/cf_token.
  await setTlsConfig(updates);
  const cfg = await getTlsConfig();

  const result = await pushTlsConfig(cfg);
  if (!result.ok) {
    const status = result.message.startsWith("missing fields:") ? 400 : 502;
    return Response.json({ ok: false, error: result.message }, { status });
  }
  return Response.json({ ok: true, enabled: cfg.enabled, domain: cfg.domain });
}
