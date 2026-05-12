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
        self._key_kill_switch = _key(self._mode, "kill_switch")
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

    # --- Daily realized-PnL persistence (audit C2)
    # The MAX_DAILY_LOSS_USD kill must survive container restarts. Without
    # this, a $400 loss followed by `docker compose restart` (which the
    # `restart: unless-stopped` policy can trigger on its own) zeroes the
    # in-memory counter and trading resumes despite blowing the cap.
    # Key: hypertrade:{mode}:daily_pnl:{YYYY-MM-DD}, TTL 48h so a date roll
    # at midnight UTC starts fresh while yesterday's record stays around
    # briefly for the daily digest.
    @staticmethod
    def _daily_pnl_key(mode: str, date_str: str) -> str:
        return f"hypertrade:{mode}:daily_pnl:{date_str}"

    async def get_daily_pnl(self, date_str: str) -> float:
        if self._redis is None:
            return 0.0
        val = await self._redis.get(self._daily_pnl_key(self._mode, date_str))
        if val is None:
            return 0.0
        try:
            parsed = float(val)
        except (TypeError, ValueError):
            return 0.0
        # Reject non-finite values. `nan < -limit` is False, which would
        # silently disable the daily-loss kill-switch if the key got
        # corrupted (e.g. hand-edited or written by a buggy version).
        import math
        if not math.isfinite(parsed):
            logger.warning(
                "Discarding non-finite daily_pnl value %r for %s",
                val, date_str,
            )
            return 0.0
        return parsed

    async def set_daily_pnl(self, date_str: str, pnl: float) -> None:
        if self._redis is None:
            return
        await self._redis.set(
            self._daily_pnl_key(self._mode, date_str),
            f"{pnl:.10f}",
            ex=48 * 3600,
        )

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

    # --- Runtime kill-switch (audit H7)
    # Pre-fix `settings.kill_switch` was env-only — flipping it required
    # `docker compose restart`, during which the running tick could still
    # place orders. The Redis-backed value is checked on every tick AND
    # is the SOURCE OF TRUTH when set; the env value remains the safe
    # default at startup so an operator-set env=true still wins.
    # Returns None when the Redis key is unset (= "use env default").

    async def is_kill_switch_active(self) -> bool | None:
        """Returns True/False when explicitly set in Redis, None when
        unset (caller should fall back to settings.kill_switch)."""
        if self._redis is None:
            return None
        val = await self._redis.get(self._key_kill_switch)
        if val is None:
            return None
        return val == "1"

    async def set_kill_switch(self, active: bool) -> None:
        if self._redis is None:
            return
        await self._redis.set(self._key_kill_switch, "1" if active else "0")
        logger.warning("Kill-switch %s via Redis", "ACTIVATED" if active else "deactivated")

    async def clear_kill_switch_override(self) -> None:
        """Remove the Redis override so `settings.kill_switch` (env) wins
        again. Useful when the operator wants to revert to the env
        default after a temporary runtime activation."""
        if self._redis is None:
            return
        await self._redis.delete(self._key_kill_switch)

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
    # NOTE: dashboard auth + TLS config (`dashboard:auth:*`,
    # `dashboard:tls:*`) used to be read/written here and exposed via
    # bot HTTP routes (`/api/auth/config`, `/api/tls/configure`, …).
    # The dashboard now owns those keys directly via
    # `dashboard/src/lib/auth-config.ts` + `tls-config.ts` (PR 4a) —
    # the bot has no need to read or mutate them, so the methods are
    # gone. The Redis keys themselves still exist and are managed
    # solely by the dashboard.

    # --- Per-strategy state snapshot (audit M6 fix completion).
    # The position-table state_json only persists state during in-position
    # windows. Cooldown-after-close needs a separate snapshot store —
    # otherwise a restart inside the cooldown window resets bars_since_close
    # to its init default (999), bypassing the 24h re-entry block.

    def _strategy_state_key(self, strategy_name: str) -> str:
        return _key(self._mode, f"strategy:{strategy_name}:state")

    async def save_strategy_state(
        self, strategy_name: str, state: dict | None,
    ) -> None:
        if self._redis is None or not state:
            return
        import json
        try:
            await self._redis.set(
                self._strategy_state_key(strategy_name), json.dumps(state),
            )
        except Exception:
            logger.exception("save_strategy_state failed for %s", strategy_name)

    async def load_strategy_state(self, strategy_name: str) -> dict | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self._strategy_state_key(strategy_name))
            if not raw:
                return None
            import json
            return json.loads(raw)
        except Exception:
            logger.exception("load_strategy_state failed for %s", strategy_name)
            return None
