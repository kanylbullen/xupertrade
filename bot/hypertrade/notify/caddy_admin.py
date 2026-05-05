"""Caddy admin-API client for dynamic config push.

The bot owns TLS configuration (stored in Redis via BotControl). When
a user toggles HTTPS on the Options page, the bot generates a full Caddy
JSON config and POSTs it to Caddy's /load endpoint, which atomically
replaces the running config and re-issues certs as needed.

Caddy admin API docs: https://caddyserver.com/docs/api
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)

CADDY_ADMIN_URL = "http://caddy:2019"


def build_https_config(
    domain: str, email: str, cf_token: str
) -> dict:
    """Build Caddy JSON config for HTTPS via Cloudflare DNS-01.

    Layout:
      :443 (HTTPS)  → reverse_proxy dashboard:3000
      :80  (HTTP)   → 308 redirect to https://{domain}
    """
    return {
        "admin": {"listen": "0.0.0.0:2019"},
        "logging": {"logs": {"default": {"level": "INFO"}}},
        "apps": {
            "tls": {
                "automation": {
                    "policies": [
                        {
                            "subjects": [domain],
                            "issuers": [
                                {
                                    "module": "acme",
                                    "email": email,
                                    "challenges": {
                                        "dns": {
                                            "provider": {
                                                "name": "cloudflare",
                                                "api_token": cf_token,
                                            }
                                        }
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
            "http": {
                "servers": {
                    "https": {
                        "listen": [":443"],
                        "routes": [
                            {
                                "match": [{"host": [domain]}],
                                "handle": [
                                    {
                                        "handler": "reverse_proxy",
                                        "upstreams": [
                                            {"dial": "dashboard:3000"}
                                        ],
                                    }
                                ],
                                "terminal": True,
                            }
                        ],
                    },
                    "http": {
                        "listen": [":80"],
                        "routes": [
                            {
                                "match": [{"host": [domain]}],
                                "handle": [
                                    {
                                        "handler": "static_response",
                                        "headers": {
                                            "Location": [f"https://{domain}{{http.request.uri}}"]
                                        },
                                        "status_code": 308,
                                    }
                                ],
                                "terminal": True,
                            }
                        ],
                    },
                }
            },
        },
    }


def build_internal_https_config(domain: str | None = None) -> dict:
    """Default config when TLS-via-LE is disabled.

    Serves HTTPS on :443 with a self-signed cert from Caddy's internal CA
    for the given domain (browser warning until the user accepts the cert).
    Plain HTTP on :80 is 308-redirected to HTTPS.

    `domain` MUST be set — Caddy needs a subject to issue for. Falls back
    to CADDY_HOST env, then "localhost" as last resort. Without a real
    subject, TLS handshake fails (ERR_SSL_PROTOCOL_ERROR).
    """
    import os
    host = (domain or os.environ.get("CADDY_HOST") or "localhost").strip()

    return {
        "admin": {"listen": "0.0.0.0:2019"},
        "apps": {
            "tls": {
                "automation": {
                    "policies": [
                        {
                            "subjects": [host],
                            "issuers": [{"module": "internal"}],
                        }
                    ]
                }
            },
            "http": {
                "servers": {
                    "https": {
                        "listen": [":443"],
                        "routes": [
                            {
                                "match": [{"host": [host]}],
                                "handle": [
                                    {
                                        "handler": "reverse_proxy",
                                        "upstreams": [
                                            {"dial": "dashboard:3000"}
                                        ],
                                    }
                                ],
                                "terminal": True,
                            }
                        ],
                    },
                    "http": {
                        "listen": [":80"],
                        "routes": [
                            {
                                "handle": [
                                    {
                                        "handler": "static_response",
                                        "headers": {
                                            "Location": ["https://{http.request.host}{http.request.uri}"]
                                        },
                                        "status_code": 308,
                                    }
                                ]
                            }
                        ],
                    },
                }
            },
        },
    }


# Back-compat alias for old callers; kept so external code/tests don't break.
build_http_only_config = build_internal_https_config


async def push_persisted_config(control) -> tuple[bool, str]:
    """Build the right Caddy config from the BotControl-persisted TLS state
    and POST it to Caddy. Centralizes the build-and-apply path so the API
    handler (Options page) and the runner's startup re-apply share one
    implementation.

    Returns (ok, message). When TLS is disabled in state, falls back to
    `build_internal_https_config` so Caddy still serves something on :443.
    """
    cfg = await control.get_tls_config()
    if cfg.get("enabled"):
        missing = [k for k in ("domain", "email", "cf_token") if not cfg.get(k)]
        if missing:
            return False, f"missing fields: {missing}"
        new_config = build_https_config(
            domain=cfg["domain"],
            email=cfg["email"],
            cf_token=cfg["cf_token"],
        )
    else:
        new_config = build_internal_https_config(domain=cfg.get("domain") or None)
    return await apply_config(new_config)


async def apply_config(config: dict) -> tuple[bool, str]:
    """POST a config to Caddy. Returns (ok, message)."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{CADDY_ADMIN_URL}/load",
                json=config,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    return True, "ok"
                text = await resp.text()
                return False, f"HTTP {resp.status}: {text[:300]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def get_status() -> dict:
    """Query Caddy's certificate manager for cert info on configured domains.

    Returns:
        reachable: bool
        tls_subjects: [hosts cert is issued for]
        issuer: "acme" | "internal" | "unknown" (from first policy)
        servers: list of server names in config
    """
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{CADDY_ADMIN_URL}/config/",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return {"reachable": False, "status": resp.status}
                data = await resp.json()
                tls_apps = data.get("apps", {}).get("tls", {})
                policies = tls_apps.get("automation", {}).get("policies", [])
                subjects: list[str] = []
                issuer = "unknown"
                for p in policies:
                    subjects.extend(p.get("subjects", []))
                    for iss in p.get("issuers", []):
                        mod = iss.get("module", "")
                        if mod == "acme":
                            issuer = "acme"
                            break
                        if mod == "internal" and issuer != "acme":
                            issuer = "internal"
                http_servers = data.get("apps", {}).get("http", {}).get("servers", {})
                return {
                    "reachable": True,
                    "tls_subjects": subjects,
                    "issuer": issuer,
                    "servers": list(http_servers.keys()),
                }
    except Exception as e:
        return {"reachable": False, "error": str(e)}
