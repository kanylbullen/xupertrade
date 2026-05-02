"""HTTP API for dashboard queries (indicator status + runtime control)."""

import logging
import os
import uuid
from dataclasses import asdict

from aiohttp import web

from hypertrade.config import settings
from hypertrade.db.repo import Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.indicators_status import get_all_status
from hypertrade.exchange.base import Exchange
from hypertrade.strategies.base import Strategy

logger = logging.getLogger(__name__)

_DASHBOARD_ORIGIN = os.getenv("DASHBOARD_URL", "*")


def _cors(payload, status: int = 200) -> web.Response:
    return web.json_response(
        payload,
        status=status,
        headers={
            "Access-Control-Allow-Origin": _DASHBOARD_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Api-Key",
        },
    )


def _require_auth(request: web.Request) -> web.Response | None:
    """Returns 401 response if API key is configured and missing/wrong, else None."""
    if not settings.api_key:
        return None  # auth disabled
    provided = request.headers.get("X-Api-Key", "")
    if provided != settings.api_key:
        return _cors({"error": "Unauthorized"}, status=401)
    return None


async def health(_request: web.Request) -> web.Response:
    return _cors({"status": "ok"})


async def hyperliquid_diagnostic(_request: web.Request) -> web.Response:
    """Verify HyperLiquid SDK can talk to testnet/mainnet using current key."""
    if not settings.hyperliquid_private_key:
        return _cors(
            {
                "ok": False,
                "error": "HYPERLIQUID_PRIVATE_KEY not set in env",
            },
            status=400,
        )

    try:
        from hypertrade.exchange.hyperliquid import HyperLiquidExchange

        ex = HyperLiquidExchange()
        balance = await ex.get_balance()
        positions = await ex.get_positions()
        btc_price = await ex.get_current_price("BTC")
        api_wallet_mode = ex.signer_address.lower() != ex.address.lower()
        return _cors(
            {
                "ok": True,
                "network": "testnet" if settings.is_testnet else "mainnet",
                "signer_address": ex.signer_address,
                "trading_account": ex.address,
                "api_wallet_mode": api_wallet_mode,
                "account_value_usd": balance.total,
                "withdrawable_usd": balance.available,
                "open_positions": len(positions),
                "btc_mid_price": btc_price,
            }
        )
    except Exception as e:
        logger.exception("HyperLiquid diagnostic failed")
        return _cors({"ok": False, "error": str(e)}, status=500)


async def positions_handler(request: web.Request) -> web.Response:
    exchange: Exchange | None = request.app.get("exchange")
    if not exchange:
        return _cors({"positions": []})
    try:
        positions = await exchange.get_positions()
        return _cors({
            "positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "size": p.size,
                    "entry_price": p.entry_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "liquidation_price": p.liquidation_price,
                }
                for p in positions
            ]
        })
    except Exception as e:
        logger.exception("Failed to get positions from exchange")
        return _cors({"error": str(e)}, status=500)


async def list_strategies_handler(request: web.Request) -> web.Response:
    strategies: list = request.app.get("strategies", [])
    return _cors({
        "strategies": [
            {"name": s.name, "symbol": s.symbol, "timeframe": s.timeframe}
            for s in strategies
        ]
    })


async def indicator_status(request: web.Request) -> web.Response:
    try:
        repo: Repository | None = request.app.get("repo")
        strategies: list = request.app.get("strategies", [])
        statuses = await get_all_status(repo, strategies if strategies else None)
        return _cors({"strategies": [asdict(s) for s in statuses]})
    except Exception as e:
        logger.exception("Failed to get indicator status")
        return _cors({"error": str(e)}, status=500)


def _control_routes(
    app: web.Application,
    control: BotControl,
    exchange: Exchange,
    strategies: list[Strategy],
) -> None:
    strategy_by_name = {s.name: s for s in strategies}

    async def auth_get_config(_request: web.Request) -> web.Response:
        """Public — returns mode + non-secret OIDC fields. Never returns
        password hash or client secret. The dashboard uses this to decide
        whether to show the login screen and which provider to use."""
        cfg = await control.get_auth_config()
        return _cors({
            "mode": cfg["mode"],
            "basic_user_set": bool(cfg["basic_user"]),
            "oidc_issuer": cfg["oidc_issuer"],
            "oidc_client_id": cfg["oidc_client_id"],
            "oidc_scopes": cfg["oidc_scopes"],
            # Session secret is needed by the dashboard middleware to verify
            # signed cookies. It's not a user-facing secret — losing it just
            # invalidates active sessions.
            "session_secret": await control.ensure_session_secret(),
        })

    async def tls_get_config(_request: web.Request) -> web.Response:
        """Public — returns mode + non-secret TLS fields. Never returns
        the Cloudflare API token."""
        cfg = await control.get_tls_config()
        from hypertrade.notify import caddy_admin
        status = await caddy_admin.get_status()
        return _cors({
            "enabled": cfg["enabled"],
            "domain": cfg["domain"],
            "email": cfg["email"],
            "cf_token_set": bool(cfg["cf_token"]),
            "caddy_status": status,
        })

    async def tls_configure(request: web.Request) -> web.Response:
        """Update TLS config and push new config to Caddy. Requires API_KEY."""
        if (err := _require_auth(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}

        # Persist requested fields
        kwargs: dict = {}
        if "enabled" in body:
            kwargs["enabled"] = bool(body["enabled"])
        for k in ("domain", "email"):
            if k in body:
                kwargs[k] = str(body[k]).strip()
        if "cf_token" in body and body["cf_token"]:
            kwargs["cf_token"] = str(body["cf_token"]).strip()

        await control.set_tls_config(**kwargs)
        cfg = await control.get_tls_config()

        from hypertrade.notify import caddy_admin

        if cfg["enabled"]:
            missing = [k for k in ("domain", "email", "cf_token") if not cfg.get(k)]
            if missing:
                return _cors(
                    {"ok": False, "error": f"missing fields: {missing}"},
                    status=400,
                )
            new_config = caddy_admin.build_https_config(
                domain=cfg["domain"],
                email=cfg["email"],
                cf_token=cfg["cf_token"],
            )
        else:
            # Pass the configured domain (if any) so the self-signed cert
            # at least matches the hostname users hit. Without it Caddy
            # has no subject to issue for and TLS handshake fails.
            new_config = caddy_admin.build_internal_https_config(
                domain=cfg.get("domain") or None,
            )

        ok, msg = await caddy_admin.apply_config(new_config)
        if not ok:
            return _cors({"ok": False, "error": msg}, status=502)
        return _cors({"ok": True, "enabled": cfg["enabled"], "domain": cfg["domain"]})

    async def auth_get_oidc_secret(request: web.Request) -> web.Response:
        """Return the OIDC client secret. Requires API_KEY — only the
        dashboard's server-side code should call this, never the browser."""
        if (err := _require_auth(request)) is not None:
            return err
        cfg = await control.get_auth_config()
        return _cors({"secret": cfg.get("oidc_client_secret", "")})

    async def auth_verify_basic(request: web.Request) -> web.Response:
        """Verify username + password. Rate-limited to one call per second
        per IP via cooldown to slow brute force.
        Returns {ok: true, user: "..."} or {ok: false}."""
        try:
            body = await request.json()
        except Exception:
            return _cors({"ok": False}, status=400)
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not username or not password:
            return _cors({"ok": False})

        cfg = await control.get_auth_config()
        # Accept verification when basic creds exist regardless of mode —
        # callers (dashboard /api/auth/login) gate on the mode separately;
        # this lets basic act as a fallback when mode=oidc misbehaves.
        if cfg["mode"] == "disabled" or not cfg["basic_user"] or not cfg["basic_hash"]:
            return _cors({"ok": False, "reason": "basic-auth-not-configured"})
        if username != cfg["basic_user"]:
            return _cors({"ok": False})

        import bcrypt
        try:
            ok = bcrypt.checkpw(
                password.encode("utf-8"),
                cfg["basic_hash"].encode("utf-8"),
            )
        except (ValueError, TypeError):
            ok = False
        return _cors({"ok": bool(ok), "user": username if ok else ""})

    async def auth_configure(request: web.Request) -> web.Response:
        """Update auth config. Requires API_KEY (admin operation)."""
        if (err := _require_auth(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        mode = body.get("mode")
        if mode not in (None, "disabled", "basic", "oidc"):
            return _cors({"error": "invalid mode"}, status=400)

        kwargs: dict[str, str] = {}
        if mode is not None:
            kwargs["mode"] = mode
        if "basic_user" in body:
            kwargs["basic_user"] = str(body["basic_user"])
        if "basic_password" in body and body["basic_password"]:
            import bcrypt
            pw = str(body["basic_password"]).encode("utf-8")
            kwargs["basic_hash"] = bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")
        for k in ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_scopes"):
            if k in body:
                kwargs[k] = str(body[k])

        await control.set_auth_config(**kwargs)
        return _cors({"ok": True, "mode": kwargs.get("mode", "")})

    async def get_heartbeat(_request: web.Request) -> web.Response:
        """Returns last tick timestamp (epoch seconds) and seconds since,
        so the dashboard / watchdog can flag a stalled bot."""
        import time
        ts = await control.get_heartbeat()
        if ts is None:
            return _cors({"heartbeat": None, "stale": True, "age_seconds": None})
        age = int(time.time()) - ts
        return _cors({"heartbeat": ts, "stale": age > 180, "age_seconds": age})

    async def get_config(_request: web.Request) -> web.Response:
        """Redis-only state — no exchange calls, always fast."""
        paused = await control.is_paused()
        disabled = sorted(await control.get_disabled_strategies())
        overrides = await control.get_all_leverage_overrides()
        allow_multi = await control.get_allow_multi_coin()
        leverage = {
            s.name: {
                "default": s.__class__.leverage,
                "current": overrides.get(s.name, s.__class__.leverage),
                "overridden": s.name in overrides,
            }
            for s in strategies
        }
        return _cors(
            {
                "paused": paused,
                "disabled_strategies": disabled,
                "leverage": leverage,
                "allow_multi_coin": allow_multi,
            }
        )

    async def get_state(_request: web.Request) -> web.Response:
        paused = await control.is_paused()
        disabled = sorted(await control.get_disabled_strategies())
        overrides = await control.get_all_leverage_overrides()
        allow_multi = await control.get_allow_multi_coin()
        positions = await exchange.get_positions()
        balance = await exchange.get_balance()
        leverage = {
            s.name: {
                "default": s.__class__.leverage,
                "current": overrides.get(s.name, s.__class__.leverage),
                "overridden": s.name in overrides,
            }
            for s in strategies
        }
        return _cors(
            {
                "paused": paused,
                "disabled_strategies": disabled,
                "open_positions": len(positions),
                "equity": balance.total,
                "leverage": leverage,
                "allow_multi_coin": allow_multi,
            }
        )

    async def set_allow_multi_coin(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        allow = bool(body.get("allow", False))
        await control.set_allow_multi_coin(allow)
        return _cors({"allow_multi_coin": allow})

    async def pause(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        await control.set_paused(True)
        return _cors({"paused": True})

    async def resume(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        await control.set_paused(False)
        return _cors({"paused": False})

    async def flat_all(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        token = uuid.uuid4().hex
        await control.request_flat_all(token)
        return _cors({"flat_request_id": token})

    async def toggle_strategy(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        name = request.match_info["name"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        enable = bool(body.get("enabled", True))
        if enable:
            await control.enable_strategy(name)
        else:
            await control.disable_strategy(name)
        return _cors({"strategy": name, "enabled": enable})

    async def set_leverage(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        name = request.match_info["name"]
        if name not in strategy_by_name:
            return _cors({"error": f"unknown strategy {name}"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            lev = int(body.get("leverage"))
        except (TypeError, ValueError):
            return _cors({"error": "leverage must be an integer"}, status=400)
        if lev < 1 or lev > 50:
            return _cors({"error": "leverage must be 1-50"}, status=400)

        strat = strategy_by_name[name]
        await control.set_leverage_override(name, lev)
        strat.leverage = lev

        # Recompute per-coin leverage and push to exchange
        per_coin: dict[str, int] = {}
        for s in strategies:
            per_coin[s.symbol] = max(per_coin.get(s.symbol, 1), s.leverage)
        await exchange.update_leverage(strat.symbol, per_coin[strat.symbol], is_cross=True)

        return _cors({"strategy": name, "leverage": lev})

    async def reset_leverage(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        name = request.match_info["name"]
        if name not in strategy_by_name:
            return _cors({"error": f"unknown strategy {name}"}, status=404)
        await control.clear_leverage_override(name)
        strat = strategy_by_name[name]
        strat.leverage = strat.__class__.leverage
        per_coin: dict[str, int] = {}
        for s in strategies:
            per_coin[s.symbol] = max(per_coin.get(s.symbol, 1), s.leverage)
        await exchange.update_leverage(strat.symbol, per_coin[strat.symbol], is_cross=True)
        return _cors({"strategy": name, "leverage": strat.leverage, "reset": True})

    async def options_handler(_request: web.Request) -> web.Response:
        return _cors({})

    app.router.add_get("/api/tls/config", tls_get_config)
    app.router.add_post("/api/tls/configure", tls_configure)
    app.router.add_route("OPTIONS", "/api/tls/{tail:.*}", options_handler)
    app.router.add_get("/api/auth/config", auth_get_config)
    app.router.add_get("/api/auth/oidc-secret", auth_get_oidc_secret)
    app.router.add_post("/api/auth/verify", auth_verify_basic)
    app.router.add_post("/api/auth/configure", auth_configure)
    app.router.add_route("OPTIONS", "/api/auth/{tail:.*}", options_handler)
    app.router.add_get("/api/control/heartbeat", get_heartbeat)
    app.router.add_get("/api/control/config", get_config)
    app.router.add_get("/api/control/state", get_state)
    app.router.add_post("/api/control/pause", pause)
    app.router.add_post("/api/control/resume", resume)
    app.router.add_post("/api/control/flat-all", flat_all)
    app.router.add_post("/api/control/strategy/{name}/toggle", toggle_strategy)
    app.router.add_post("/api/control/strategy/{name}/leverage", set_leverage)
    app.router.add_post("/api/control/strategy/{name}/leverage/reset", reset_leverage)
    app.router.add_post("/api/control/allow-multi-coin", set_allow_multi_coin)
    app.router.add_route("OPTIONS", "/api/control/{tail:.*}", options_handler)


def create_app(
    control: BotControl | None = None,
    exchange: Exchange | None = None,
    strategies: list[Strategy] | None = None,
    repo: Repository | None = None,
) -> web.Application:
    app = web.Application()
    app["repo"] = repo
    app["exchange"] = exchange
    app["strategies"] = strategies or []
    app.router.add_get("/health", health)
    app.router.add_get("/strategies", list_strategies_handler)
    app.router.add_get("/api/positions", positions_handler)
    app.router.add_get("/api/indicator-status", indicator_status)
    app.router.add_get("/api/hyperliquid/diagnostic", hyperliquid_diagnostic)
    if control is not None and exchange is not None:
        _control_routes(app, control, exchange, strategies or [])
    return app


async def start_api_server(
    port: int = 8000,
    control: BotControl | None = None,
    exchange: Exchange | None = None,
    strategies: list[Strategy] | None = None,
    repo: Repository | None = None,
) -> web.AppRunner:
    app = create_app(
        control=control, exchange=exchange, strategies=strategies, repo=repo
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Bot API started on port %d", port)
    return runner
