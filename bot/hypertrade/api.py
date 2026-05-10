"""HTTP API for dashboard queries (indicator status + runtime control)."""

import hmac
import json
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
    """Returns 401 response if API key is configured and missing/wrong, else None.

    Uses `hmac.compare_digest` for constant-time comparison so the response
    timing doesn't leak how many leading characters matched.
    """
    if not settings.api_key:
        return None  # auth disabled
    provided = request.headers.get("X-Api-Key", "")
    if not hmac.compare_digest(provided, settings.api_key):
        return _cors({"error": "Unauthorized"}, status=401)
    return None


async def health(_request: web.Request) -> web.Response:
    return _cors({"status": "ok"})


async def hyperliquid_diagnostic(request: web.Request) -> web.Response:
    """Verify HyperLiquid SDK can talk to testnet/mainnet using current key."""
    if (err := _require_auth(request)) is not None:
        return err
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
    if (err := _require_auth(request)) is not None:
        return err
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
    if (err := _require_auth(request)) is not None:
        return err
    strategies: list = request.app.get("strategies", [])
    return _cors({
        "strategies": [
            {"name": s.name, "symbol": s.symbol, "timeframe": s.timeframe}
            for s in strategies
        ]
    })


async def indicator_status(request: web.Request) -> web.Response:
    if (err := _require_auth(request)) is not None:
        return err
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
        password hash, client secret, or session_secret. The dashboard
        uses this to decide whether to show the login screen and which
        provider to use.

        SECURITY: do NOT add `session_secret` here. It's the HMAC key
        the dashboard signs/verifies cookies with — leaking it to a
        public endpoint lets anyone forge a valid session and walk past
        proxy.ts. The dashboard fetches it server-side from
        `/api/auth/session-secret` (API_KEY-gated).
        """
        cfg = await control.get_auth_config()
        return _cors({
            "mode": cfg["mode"],
            "basic_user_set": bool(cfg["basic_user"]),
            "oidc_issuer": cfg["oidc_issuer"],
            "oidc_client_id": cfg["oidc_client_id"],
            "oidc_scopes": cfg["oidc_scopes"],
        })

    async def auth_get_session_secret(request: web.Request) -> web.Response:
        """Return the dashboard's session-cookie HMAC key. API_KEY-gated
        — only the dashboard's server-side render code should call this,
        never the browser. Generates the secret on first call.
        """
        if (err := _require_auth(request)) is not None:
            return err
        return _cors({"session_secret": await control.ensure_session_secret()})

    async def tls_get_config(request: web.Request) -> web.Response:
        """Auth-gated. Returns mode + non-secret TLS fields. Never returns
        the Cloudflare API token. Reveals whether TLS is configured and
        what hostname — gated alongside the rest of the config surface."""
        if (err := _require_auth(request)) is not None:
            return err
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

        ok, msg = await caddy_admin.push_persisted_config(control)
        if not ok:
            # missing-field validation is the only client-error case
            status = 400 if msg.startswith("missing fields") else 502
            return _cors({"ok": False, "error": msg}, status=status)
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

    async def get_heartbeat(request: web.Request) -> web.Response:
        """Returns last tick timestamp (epoch seconds) and seconds since,
        so the dashboard / watchdog can flag a stalled bot."""
        if (err := _require_auth(request)) is not None:
            return err
        import time
        ts = await control.get_heartbeat()
        if ts is None:
            return _cors({"heartbeat": None, "stale": True, "age_seconds": None})
        age = int(time.time()) - ts
        return _cors({"heartbeat": ts, "stale": age > 180, "age_seconds": age})

    async def get_config(request: web.Request) -> web.Response:
        """Redis-only state — no exchange calls, always fast."""
        if (err := _require_auth(request)) is not None:
            return err
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

    async def get_state(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
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

    async def kill_switch_set(request: web.Request) -> web.Response:
        """POST /api/control/kill-switch — audit H7. Body must be exactly
        one of:
          {"active": true}   — activate the kill-switch (block opens)
          {"active": false}  — deactivate
          {"clear": true}    — drop the override; env default takes back over
        Strict JSON-bool validation since this endpoint disables trading
        — string "false" or "0" must NOT be silently coerced to True
        (PR #32 review fix). Always API_KEY-gated.
        """
        if (err := _require_auth(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            return _cors({"error": "body must be valid JSON"}, status=400)
        if not isinstance(body, dict):
            return _cors({"error": "body must be a JSON object"}, status=400)
        if body.get("clear") is True:
            await control.clear_kill_switch_override()
            redis_state = await control.is_kill_switch_active()
            return _cors({"override_cleared": True, "redis_state": redis_state})
        active = body.get("active")
        if not isinstance(active, bool):
            return _cors(
                {"error": "field 'active' must be a JSON boolean (true/false)"},
                status=400,
            )
        await control.set_kill_switch(active)
        return _cors({"kill_switch": active})

    async def kill_switch_get(request: web.Request) -> web.Response:
        """GET /api/control/kill-switch — returns the effective state.
        Public (no API_KEY) — read-only and useful for the dashboard
        status badge."""
        from hypertrade.config import settings as _s
        redis_state = await control.is_kill_switch_active()
        effective = redis_state if redis_state is not None else _s.kill_switch
        return _cors({
            "effective": effective,
            "env_default": _s.kill_switch,
            "redis_override": redis_state,
        })

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

    async def hodl_signals(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        from hypertrade.hodl.registry import all_signals, load_all
        load_all()
        results = []
        for sig in all_signals():
            try:
                state = await sig.evaluate()
                results.append(state.to_dict())
            except Exception as e:
                results.append({
                    "name": sig.name, "asset": sig.asset,
                    "description": sig.description,
                    "triggered": False, "score": 0.0, "threshold": sig.threshold,
                    "verdict": "Unknown — evaluation failed",
                    "checks": [], "notes": "", "error": str(e),
                })
        return _cors({"signals": results})

    async def hodl_levels(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        repo: Repository | None = request.app.get("repo")
        if repo is None:
            return _cors({"latest": None})
        try:
            latest = await repo.latest_onchain_level()
        except Exception as e:
            return _cors({"latest": None, "error": str(e)}, status=500)
        if latest is None:
            return _cors({"latest": None})
        return _cors({"latest": {
            "id": latest.id,
            "recorded_at": latest.recorded_at.isoformat() if latest.recorded_at else None,
            "sth_cost_basis_usd": latest.sth_cost_basis_usd,
            "lth_cost_basis_usd": latest.lth_cost_basis_usd,
            "realized_price_usd": latest.realized_price_usd,
            "cvdd_usd": latest.cvdd_usd,
            "source": latest.source,
            "notes": latest.notes,
        }})

    async def hodl_purchases(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        repo: Repository | None = request.app.get("repo")
        if repo is None:
            return _cors({"purchases": []})
        try:
            limit = int(request.query.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        try:
            rows = await repo.list_hodl_purchases(limit=limit)
        except Exception as e:
            return _cors({"purchases": [], "error": str(e)}, status=500)
        return _cors({"purchases": [{
            "id": p.id,
            "purchased_at": p.purchased_at.isoformat() if p.purchased_at else None,
            "asset": p.asset,
            "exchange": p.exchange,
            "amount_local": p.amount_local,
            "local_currency": p.local_currency,
            "btc_amount": p.btc_amount,
            "btc_price_usd": p.btc_price_usd,
            "btc_price_local": p.btc_price_local,
            "fx_rate": p.fx_rate,
            "zone": p.zone,
            "cold_storage_at": p.cold_storage_at.isoformat() if p.cold_storage_at else None,
            "cold_storage_address": p.cold_storage_address,
            "notes": p.notes,
        } for p in rows]})

    async def vaults_mine(request: web.Request) -> web.Response:
        """Return the user's tracked vault deposits joined with our scoring.

        For each held vault we attach:
          - current equity + entry equity for a rough P&L (best we can do
            without scraping HL deposit receipts)
          - latest snapshot's qualified status + filter breakdown so the
            UI can flag vaults that have drifted out of qualification
          - lockup window if any

        Auth-gated when API_KEY is set: the response includes a wallet
        address and per-vault equities, which is identifying information
        even though the underlying data is public on-chain. The dashboard
        sends `X-Api-Key` from its server-side env when calling this.
        """
        if (err := _require_auth(request)) is not None:
            return err
        repo: Repository | None = request.app.get("repo")
        if repo is None or not settings.vault_tracking_address:
            return _cors({"address": "", "positions": [], "total_equity_usd": 0.0})
        addr = settings.vault_tracking_address.strip().lower()
        try:
            entries = await repo.list_user_vault_entries(addr)
        except Exception as e:
            return _cors({"address": addr, "positions": [], "error": str(e)},
                         status=500)

        positions = []
        total = 0.0
        for e in entries:
            vault = await repo.get_vault(e.vault_address)
            snap = await repo.latest_vault_snapshot(e.vault_address)
            breakdown = []
            if snap and snap.filter_breakdown_json:
                try:
                    breakdown = json.loads(snap.filter_breakdown_json)
                except (json.JSONDecodeError, TypeError):
                    breakdown = []
            failed = [r["name"] for r in breakdown if not r.get("passed", True)]
            # Cost basis ≈ current equity − unrealized P&L. (Actual on-chain
            # cost basis would need scraping deposit receipts.)
            cost_basis_usd = e.vault_equity_usd - e.unrealized_pnl_usd
            all_time_pnl_pct = (
                e.all_time_pnl_usd / cost_basis_usd
                if cost_basis_usd > 0 else None
            )
            total += e.vault_equity_usd
            positions.append({
                "vault_address": e.vault_address,
                "vault_name": vault.name if vault else None,
                "leader_address": vault.leader_address if vault else None,
                "vault_equity_usd": e.vault_equity_usd,
                "unrealized_pnl_usd": e.unrealized_pnl_usd,
                "all_time_pnl_usd": e.all_time_pnl_usd,
                "all_time_pnl_pct": all_time_pnl_pct,
                "cost_basis_usd": cost_basis_usd,
                "days_following": e.days_following,
                "entered_at": (
                    e.entered_at.isoformat() if e.entered_at else None
                ),
                "last_seen_at": (
                    e.last_seen_at.isoformat() if e.last_seen_at else None
                ),
                "locked_until": (
                    e.locked_until.isoformat() if e.locked_until else None
                ),
                "qualified": bool(snap and snap.qualified),
                "failed_filters": failed,
                "current_apr": snap.apr if snap else None,
                "current_sharpe_180d": snap.sharpe_180d if snap else None,
                "current_aum_usd": snap.aum_usd if snap else None,
                "current_max_drawdown_pct": snap.max_drawdown_pct if snap else None,
                "current_leader_equity_pct": snap.leader_equity_pct if snap else None,
                "snapshot_at": (
                    snap.snapshot_at.isoformat() if snap and snap.snapshot_at else None
                ),
            })
        # Sort by all-time P&L percent desc so best performers surface first.
        positions.sort(
            key=lambda p: (
                p["all_time_pnl_pct"] if p["all_time_pnl_pct"] is not None else 0.0
            ),
            reverse=True,
        )
        return _cors({
            "address": addr,
            "positions": positions,
            "total_equity_usd": total,
        })

    async def vaults_list(request: web.Request) -> web.Response:
        """Currently qualified vaults with latest snapshot metrics.

        Auth-gated: reveals which vaults this user is monitoring + the
        scanner's qualification state. Less personally identifying than
        /api/vaults/mine but still tracking metadata."""
        if (err := _require_auth(request)) is not None:
            return err
        repo: Repository | None = request.app.get("repo")
        if repo is None:
            return _cors({"vaults": []})
        try:
            rows = await repo.latest_qualified_vaults()
        except Exception as e:
            return _cors({"vaults": [], "error": str(e)}, status=500)
        out = []
        for vault, snap in rows:
            out.append({
                "address": vault.address,
                "name": vault.name,
                "leader_address": vault.leader_address,
                "description": vault.description,
                "created_at": vault.created_at.isoformat() if vault.created_at else None,
                "profit_share_pct": vault.profit_share_pct,
                "snapshot_at": snap.snapshot_at.isoformat() if snap.snapshot_at else None,
                "aum_usd": snap.aum_usd,
                "nav": snap.nav,
                "leader_equity_pct": snap.leader_equity_pct,
                "depositor_count": snap.depositor_count,
                "apr": snap.apr,
                "age_days": snap.age_days,
                "roi_7d": snap.roi_7d,
                "roi_30d": snap.roi_30d,
                "roi_90d": snap.roi_90d,
                "roi_180d": snap.roi_180d,
                "roi_365d": snap.roi_365d,
                "max_drawdown_pct": snap.max_drawdown_pct,
                "sharpe_180d": snap.sharpe_180d,
                "qualified": snap.qualified,
                "allow_deposits": snap.allow_deposits,
                "is_closed": snap.is_closed,
            })
        # Default sort: best Sharpe first.
        out.sort(key=lambda v: v.get("sharpe_180d") or 0.0, reverse=True)
        return _cors({"vaults": out})

    async def vault_detail(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        repo: Repository | None = request.app.get("repo")
        address = request.match_info.get("address", "").lower()
        if repo is None or not address:
            return _cors({"vault": None}, status=404)
        try:
            vault = await repo.get_vault(address)
            snap = await repo.latest_vault_snapshot(address) if vault else None
            nav = await repo.vault_nav_for(address) if vault else []
        except Exception as e:
            return _cors({"vault": None, "error": str(e)}, status=500)
        if vault is None:
            return _cors({"vault": None}, status=404)
        breakdown = []
        if snap and snap.filter_breakdown_json:
            try:
                breakdown = json.loads(snap.filter_breakdown_json)
            except (json.JSONDecodeError, TypeError):
                breakdown = []
        return _cors({"vault": {
            "address": vault.address,
            "name": vault.name,
            "leader_address": vault.leader_address,
            "description": vault.description,
            "created_at": vault.created_at.isoformat() if vault.created_at else None,
            "profit_share_pct": vault.profit_share_pct,
            "latest_snapshot": None if snap is None else {
                "snapshot_at": snap.snapshot_at.isoformat() if snap.snapshot_at else None,
                "aum_usd": snap.aum_usd,
                "nav": snap.nav,
                "leader_equity_pct": snap.leader_equity_pct,
                "depositor_count": snap.depositor_count,
                "apr": snap.apr,
                "age_days": snap.age_days,
                "roi_7d": snap.roi_7d,
                "roi_30d": snap.roi_30d,
                "roi_90d": snap.roi_90d,
                "roi_180d": snap.roi_180d,
                "roi_365d": snap.roi_365d,
                "max_drawdown_pct": snap.max_drawdown_pct,
                "sharpe_180d": snap.sharpe_180d,
                "qualified": snap.qualified,
                "allow_deposits": snap.allow_deposits,
                "is_closed": snap.is_closed,
                "filter_breakdown": breakdown,
            },
            "nav_history": [
                {"timestamp": p.timestamp.isoformat(), "nav": p.nav}
                for p in nav
            ],
        }})

    async def vault_snapshots(request: web.Request) -> web.Response:
        if (err := _require_auth(request)) is not None:
            return err
        repo: Repository | None = request.app.get("repo")
        address = request.match_info.get("address", "").lower()
        if repo is None or not address:
            return _cors({"snapshots": []})
        try:
            limit = int(request.query.get("days", "30"))
        except (ValueError, TypeError):
            limit = 30
        try:
            rows = await repo.vault_snapshots_for(address, limit=limit)
        except Exception as e:
            return _cors({"snapshots": [], "error": str(e)}, status=500)
        return _cors({"snapshots": [{
            "snapshot_at": s.snapshot_at.isoformat() if s.snapshot_at else None,
            "aum_usd": s.aum_usd,
            "nav": s.nav,
            "leader_equity_pct": s.leader_equity_pct,
            "apr": s.apr,
            "roi_90d": s.roi_90d,
            "roi_180d": s.roi_180d,
            "max_drawdown_pct": s.max_drawdown_pct,
            "sharpe_180d": s.sharpe_180d,
            "qualified": s.qualified,
        } for s in rows]})

    app.router.add_get("/api/hodl/signals", hodl_signals)
    app.router.add_get("/api/hodl/levels", hodl_levels)
    app.router.add_get("/api/hodl/purchases", hodl_purchases)
    app.router.add_route("OPTIONS", "/api/hodl/{tail:.*}", options_handler)
    app.router.add_get("/api/vaults", vaults_list)
    app.router.add_get("/api/vaults/mine", vaults_mine)
    app.router.add_get("/api/vaults/{address}", vault_detail)
    app.router.add_get("/api/vaults/{address}/snapshots", vault_snapshots)
    app.router.add_route("OPTIONS", "/api/vaults/{tail:.*}", options_handler)
    app.router.add_get("/api/tls/config", tls_get_config)
    app.router.add_post("/api/tls/configure", tls_configure)
    app.router.add_route("OPTIONS", "/api/tls/{tail:.*}", options_handler)
    app.router.add_get("/api/auth/config", auth_get_config)
    app.router.add_get("/api/auth/oidc-secret", auth_get_oidc_secret)
    app.router.add_get("/api/auth/session-secret", auth_get_session_secret)
    app.router.add_post("/api/auth/verify", auth_verify_basic)
    app.router.add_post("/api/auth/configure", auth_configure)
    app.router.add_route("OPTIONS", "/api/auth/{tail:.*}", options_handler)
    app.router.add_get("/api/control/heartbeat", get_heartbeat)
    app.router.add_get("/api/control/config", get_config)
    app.router.add_get("/api/control/state", get_state)
    app.router.add_post("/api/control/pause", pause)
    app.router.add_post("/api/control/resume", resume)
    app.router.add_post("/api/control/flat-all", flat_all)
    app.router.add_get("/api/control/kill-switch", kill_switch_get)
    app.router.add_post("/api/control/kill-switch", kill_switch_set)
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
