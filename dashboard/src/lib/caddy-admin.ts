/**
 * Caddy admin API client (PR 4a).
 *
 * TypeScript port of `bot/hypertrade/notify/caddy_admin.py`. Builds
 * Caddy JSON config (HTTPS via Let's Encrypt + Cloudflare DNS-01,
 * or self-signed internal CA when LE is disabled) and POSTs to
 * Caddy's admin API on the internal docker network.
 *
 * Same dial target (`dashboard:3000`) — the dashboard is what
 * Caddy reverse-proxies to. Same JSON shape as the bot's
 * `build_https_config` + `build_internal_https_config`, so when
 * PR 4c removes the bot-side handler the behaviour is byte-identical.
 *
 * Reachable on the compose network as `caddy:2019`. Override via
 * `CADDY_ADMIN_URL` for non-default deploys.
 */

// Server-only — the Caddy admin API at caddy:2019 is only
// reachable from inside the docker network, but `server-only`
// also prevents this module from being accidentally imported
// into a Client Component (where the fetch would target the
// browser's network and 404).
import "server-only";

const CADDY_ADMIN_URL =
  process.env.CADDY_ADMIN_URL || "http://caddy:2019";

const CADDY_LOAD_TIMEOUT_MS = 15_000;

export type CaddyApplyResult =
  | { ok: true; message: "ok" }
  | { ok: false; message: string };

export function buildHttpsConfig(args: {
  domain: string;
  email: string;
  cfToken: string;
}): unknown {
  return {
    admin: { listen: "0.0.0.0:2019" },
    logging: { logs: { default: { level: "INFO" } } },
    apps: {
      tls: {
        automation: {
          policies: [
            {
              subjects: [args.domain],
              issuers: [
                {
                  module: "acme",
                  email: args.email,
                  challenges: {
                    dns: {
                      provider: {
                        name: "cloudflare",
                        api_token: args.cfToken,
                      },
                    },
                  },
                },
              ],
            },
          ],
        },
      },
      http: {
        servers: {
          https: {
            listen: [":443"],
            routes: [
              {
                match: [{ host: [args.domain] }],
                handle: [
                  {
                    handler: "reverse_proxy",
                    upstreams: [{ dial: "dashboard:3000" }],
                  },
                ],
                terminal: true,
              },
            ],
          },
          http: {
            listen: [":80"],
            routes: [
              {
                match: [{ host: [args.domain] }],
                handle: [
                  {
                    handler: "static_response",
                    headers: {
                      Location: [
                        `https://${args.domain}{http.request.uri}`,
                      ],
                    },
                    status_code: 308,
                  },
                ],
                terminal: true,
              },
            ],
          },
        },
      },
    },
  };
}

/**
 * Self-signed bootstrap config (used when LE is disabled). The
 * `subjects` field is REQUIRED — without it Caddy doesn't know
 * which SNI to issue for and TLS handshake fails
 * (ERR_SSL_PROTOCOL_ERROR). Falls back to CADDY_HOST env, then
 * "localhost" as last resort.
 */
export function buildInternalHttpsConfig(
  domain?: string | null,
): unknown {
  const host = (
    domain ||
    process.env.CADDY_HOST ||
    "localhost"
  ).trim();

  return {
    admin: { listen: "0.0.0.0:2019" },
    apps: {
      tls: {
        automation: {
          policies: [
            {
              subjects: [host],
              issuers: [{ module: "internal" }],
            },
          ],
        },
      },
      http: {
        servers: {
          https: {
            listen: [":443"],
            routes: [
              {
                match: [{ host: [host] }],
                handle: [
                  {
                    handler: "reverse_proxy",
                    upstreams: [{ dial: "dashboard:3000" }],
                  },
                ],
                terminal: true,
              },
            ],
          },
          http: {
            listen: [":80"],
            routes: [
              {
                handle: [
                  {
                    handler: "static_response",
                    headers: {
                      Location: [
                        "https://{http.request.host}{http.request.uri}",
                      ],
                    },
                    status_code: 308,
                  },
                ],
              },
            ],
          },
        },
      },
    },
  };
}

/**
 * POST a config to Caddy. Returns {ok, message}.
 *
 * 15s timeout matches the bot's existing client — Caddy's
 * /load can take a few seconds when LE certs are being requested
 * for the first time.
 */
export async function applyCaddyConfig(
  config: unknown,
): Promise<CaddyApplyResult> {
  try {
    const res = await fetch(`${CADDY_ADMIN_URL}/load`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(config),
      signal: AbortSignal.timeout(CADDY_LOAD_TIMEOUT_MS),
    });
    if (res.status === 200) return { ok: true, message: "ok" };
    const text = await res.text().catch(() => "");
    return { ok: false, message: `HTTP ${res.status}: ${text.slice(0, 300)}` };
  } catch (e) {
    const name = e instanceof Error ? e.name : "Error";
    const msg = e instanceof Error ? e.message : String(e);
    return { ok: false, message: `${name}: ${msg}` };
  }
}

export type CaddyStatus =
  | {
      reachable: true;
      tls_subjects: string[];
      issuer: "acme" | "internal" | "unknown";
      servers: string[];
    }
  | { reachable: false; error?: string; status?: number };

/**
 * Query Caddy's running config for cert subjects + issuer.
 * Mirrors `caddy_admin.py:get_status`. Used by the Options page
 * TLS card to surface "issued for" + issuer state.
 */
export async function getCaddyStatus(): Promise<CaddyStatus> {
  try {
    const res = await fetch(`${CADDY_ADMIN_URL}/config/`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.status !== 200) {
      return { reachable: false, status: res.status };
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const data = (await res.json()) as any;
    const policies = data?.apps?.tls?.automation?.policies ?? [];
    const subjects: string[] = [];
    let issuer: "acme" | "internal" | "unknown" = "unknown";
    for (const p of policies) {
      if (Array.isArray(p?.subjects)) subjects.push(...p.subjects);
      for (const iss of p?.issuers ?? []) {
        const mod = iss?.module;
        if (mod === "acme") {
          issuer = "acme";
          break;
        }
        if (mod === "internal" && issuer !== "acme") {
          issuer = "internal";
        }
      }
    }
    const httpServers = data?.apps?.http?.servers ?? {};
    return {
      reachable: true,
      tls_subjects: subjects,
      issuer,
      servers: Object.keys(httpServers),
    };
  } catch (e) {
    return {
      reachable: false,
      error: e instanceof Error ? e.message : String(e),
    };
  }
}

/**
 * Build the right config from the current TlsConfig and apply it.
 * Mirrors `push_persisted_config` in caddy_admin.py.
 *
 * When enabled=true but a required field is missing, returns an
 * error WITHOUT applying anything — better to keep the previous
 * Caddy state than to half-configure it.
 */
export async function pushTlsConfig(cfg: {
  enabled: boolean;
  domain: string;
  email: string;
  cf_token: string;
}): Promise<CaddyApplyResult> {
  if (cfg.enabled) {
    const missing: string[] = [];
    if (!cfg.domain) missing.push("domain");
    if (!cfg.email) missing.push("email");
    if (!cfg.cf_token) missing.push("cf_token");
    if (missing.length > 0) {
      return {
        ok: false,
        message: `missing fields: ${missing.join(", ")}`,
      };
    }
    return applyCaddyConfig(
      buildHttpsConfig({
        domain: cfg.domain,
        email: cfg.email,
        cfToken: cfg.cf_token,
      }),
    );
  }
  return applyCaddyConfig(
    buildInternalHttpsConfig(cfg.domain || null),
  );
}
