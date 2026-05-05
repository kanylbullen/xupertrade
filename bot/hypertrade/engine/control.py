"""Runtime bot control state stored in Redis.

Lets the dashboard pause/resume the bot, toggle individual strategies,
and request a 'flat all' close-all-positions action.

State keys:
- hypertrade:control:paused           -> "1" or "0"
- hypertrade:control:disabled         -> SET of strategy names that are OFF
- hypertrade:control:flat_request_id  -> opaque token; bot acts on each
                                        new value, then writes the same
                                        token to flat_request_done
- hypertrade:control:flat_request_done
"""

import logging

import redis.asyncio as redis

from hypertrade.config import settings

logger = logging.getLogger(__name__)

def _key(mode: str, suffix: str) -> str:
    return f"hypertrade:{mode}:control:{suffix}"


class BotControl:
    def __init__(self, redis_url: str | None = None, mode: str | None = None) -> None:
        self._redis_url = redis_url or settings.redis_url
        self._mode = mode or settings.exchange_mode
        self._key_paused = _key(self._mode, "paused")
        self._key_disabled = _key(self._mode, "disabled")
        self._key_flat_req = _key(self._mode, "flat_request_id")
        self._key_flat_done = _key(self._mode, "flat_request_done")
        self._key_leverage = _key(self._mode, "leverage")
        self._key_allow_multi = _key(self._mode, "allow_multi_coin")
        self._key_heartbeat = _key(self._mode, "heartbeat")
        self._redis: redis.Redis | None = None

    async def connect(self) -> None:
        self._redis = redis.from_url(self._redis_url, decode_responses=True)

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()

    async def is_paused(self) -> bool:
        if self._redis is None:
            return False
        val = await self._redis.get(self._key_paused)
        return val == "1"

    async def set_paused(self, paused: bool) -> None:
        if self._redis is None:
            return
        await self._redis.set(self._key_paused, "1" if paused else "0")
        logger.info("Bot %s via control", "paused" if paused else "resumed")

    async def is_strategy_enabled(self, name: str) -> bool:
        if self._redis is None:
            return True
        return not await self._redis.sismember(self._key_disabled, name)

    async def get_disabled_strategies(self) -> set[str]:
        if self._redis is None:
            return set()
        members = await self._redis.smembers(self._key_disabled)
        return set(members or [])

    async def enable_strategy(self, name: str) -> None:
        if self._redis is None:
            return
        await self._redis.srem(self._key_disabled, name)
        logger.info("Strategy %s enabled via control", name)

    async def disable_strategy(self, name: str) -> None:
        if self._redis is None:
            return
        await self._redis.sadd(self._key_disabled, name)
        logger.info("Strategy %s disabled via control", name)

    async def request_flat_all(self, token: str) -> None:
        """Set the request token. Bot will detect new token and act."""
        if self._redis is None:
            return
        await self._redis.set(self._key_flat_req, token)
        logger.info("Flat-all requested with token %s", token)

    async def get_pending_flat_request(self) -> str | None:
        """Return the request token if a flat-all is pending (not yet processed)."""
        if self._redis is None:
            return None
        req = await self._redis.get(self._key_flat_req)
        if not req:
            return None
        done = await self._redis.get(self._key_flat_done)
        if req == done:
            return None
        return req

    async def acknowledge_flat_request(self, token: str) -> None:
        if self._redis is None:
            return
        await self._redis.set(self._key_flat_done, token)

    async def get_leverage_override(self, strategy_name: str) -> int | None:
        if self._redis is None:
            return None
        val = await self._redis.hget(self._key_leverage, strategy_name)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    async def get_all_leverage_overrides(self) -> dict[str, int]:
        if self._redis is None:
            return {}
        raw = await self._redis.hgetall(self._key_leverage)
        out: dict[str, int] = {}
        for k, v in (raw or {}).items():
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    async def set_leverage_override(self, strategy_name: str, leverage: int) -> None:
        if self._redis is None:
            return
        await self._redis.hset(self._key_leverage, strategy_name, str(int(leverage)))
        logger.info("Leverage override set: %s = %dx", strategy_name, leverage)

    async def clear_leverage_override(self, strategy_name: str) -> None:
        if self._redis is None:
            return
        await self._redis.hdel(self._key_leverage, strategy_name)

    async def get_allow_multi_coin(self) -> bool:
        if self._redis is None:
            return False
        val = await self._redis.get(self._key_allow_multi)
        return val == "1"

    async def set_allow_multi_coin(self, allow: bool) -> None:
        if self._redis is None:
            return
        await self._redis.set(self._key_allow_multi, "1" if allow else "0")
        logger.info("allow_multi_coin set to %s", allow)

    async def beat_heartbeat(self) -> None:
        """Write current timestamp + TTL of 5 minutes. A watchdog reads this
        and alerts if it's stale or missing."""
        if self._redis is None:
            return
        import time
        await self._redis.set(
            self._key_heartbeat, str(int(time.time())), ex=300
        )

    async def get_heartbeat(self) -> int | None:
        if self._redis is None:
            return None
        val = await self._redis.get(self._key_heartbeat)
        if val is None:
            return None
        try:
            return int(val)
        except ValueError:
            return None

    # --- Dashboard authentication config (NOT mode-scoped — global to bot)
    # Keys:
    #   dashboard:auth:mode       — "disabled" | "basic" | "oidc"
    #   dashboard:auth:basic:user
    #   dashboard:auth:basic:hash — bcrypt hash of password
    #   dashboard:auth:session_secret  — random string used to sign cookies
    #   dashboard:auth:oidc:* (issuer, client_id, client_secret, scopes)

    async def get_auth_config(self) -> dict:
        if self._redis is None:
            return {"mode": "disabled"}
        keys = [
            "dashboard:auth:mode",
            "dashboard:auth:basic:user",
            "dashboard:auth:basic:hash",
            "dashboard:auth:session_secret",
            "dashboard:auth:oidc:issuer",
            "dashboard:auth:oidc:client_id",
            "dashboard:auth:oidc:client_secret",
            "dashboard:auth:oidc:scopes",
        ]
        vals = await self._redis.mget(*keys)
        return {
            "mode": vals[0] or "disabled",
            "basic_user": vals[1] or "",
            "basic_hash": vals[2] or "",
            "session_secret": vals[3] or "",
            "oidc_issuer": vals[4] or "",
            "oidc_client_id": vals[5] or "",
            "oidc_client_secret": vals[6] or "",
            "oidc_scopes": vals[7] or "openid profile email",
        }

    async def set_auth_config(self, **kwargs: str) -> None:
        if self._redis is None:
            return
        mapping = {
            "mode": "dashboard:auth:mode",
            "basic_user": "dashboard:auth:basic:user",
            "basic_hash": "dashboard:auth:basic:hash",
            "session_secret": "dashboard:auth:session_secret",
            "oidc_issuer": "dashboard:auth:oidc:issuer",
            "oidc_client_id": "dashboard:auth:oidc:client_id",
            "oidc_client_secret": "dashboard:auth:oidc:client_secret",
            "oidc_scopes": "dashboard:auth:oidc:scopes",
        }
        pipe = self._redis.pipeline()
        for arg, key in mapping.items():
            if arg in kwargs:
                val = kwargs[arg]
                if val is None or val == "":
                    pipe.delete(key)
                else:
                    pipe.set(key, val)
        await pipe.execute()

    # --- TLS / HTTPS configuration (Caddy reverse proxy)
    # Keys:
    #   dashboard:tls:enabled    — "0" or "1"
    #   dashboard:tls:domain     — e.g. "hypertrade.xuper.fun"
    #   dashboard:tls:email      — for Let's Encrypt notifications
    #   dashboard:tls:cf_token   — Cloudflare API token (Zone:Read + Zone DNS:Edit)

    async def get_tls_config(self) -> dict:
        if self._redis is None:
            return {"enabled": False, "domain": "", "email": "", "cf_token": ""}
        keys = [
            "dashboard:tls:enabled",
            "dashboard:tls:domain",
            "dashboard:tls:email",
            "dashboard:tls:cf_token",
        ]
        vals = await self._redis.mget(*keys)
        return {
            "enabled": vals[0] == "1",
            "domain": vals[1] or "",
            "email": vals[2] or "",
            "cf_token": vals[3] or "",
        }

    async def set_tls_config(self, **kwargs: str) -> None:
        if self._redis is None:
            return
        mapping = {
            "enabled": "dashboard:tls:enabled",
            "domain": "dashboard:tls:domain",
            "email": "dashboard:tls:email",
            "cf_token": "dashboard:tls:cf_token",
        }
        pipe = self._redis.pipeline()
        for arg, key in mapping.items():
            if arg in kwargs:
                val = kwargs[arg]
                if arg == "enabled":
                    pipe.set(key, "1" if val else "0")
                elif val is None or val == "":
                    pipe.delete(key)
                else:
                    pipe.set(key, val)
        await pipe.execute()

    async def cache_get(self, key: str) -> str | None:
        """Read a generic Redis cache key. Returns None on miss/error."""
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception:
            return None

    async def cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Write a generic Redis cache key with TTL. Silent on error."""
        if self._redis is None:
            return
        try:
            await self._redis.set(key, value, ex=ttl_seconds)
        except Exception:
            pass

    async def ensure_session_secret(self) -> str:
        """Generate session_secret if missing. Returns the current secret."""
        if self._redis is None:
            return ""
        cur = await self._redis.get("dashboard:auth:session_secret")
        if cur:
            return cur
        import secrets
        new = secrets.token_urlsafe(48)
        await self._redis.set("dashboard:auth:session_secret", new)
        return new
