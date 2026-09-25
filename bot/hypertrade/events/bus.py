"""Redis pub/sub event bus."""

import logging

import redis.asyncio as redis

from hypertrade.config import settings
from hypertrade.events.types import Event

logger = logging.getLogger(__name__)

CHANNEL_BASE = "hypertrade:events"
# All-modes channel — every event published here regardless of mode.
# Per-mode channel: hypertrade:{mode}:events
CHANNEL = CHANNEL_BASE  # legacy fallback; new code uses per-mode channel below


def channel_for(mode: str) -> str:
    return f"hypertrade:{mode}:events"


class EventBus:
    def __init__(self, redis_url: str | None = None, mode: str | None = None) -> None:
        self._redis_url = redis_url or settings.redis_url
        self._mode = mode or settings.exchange_mode
        self._channel = channel_for(self._mode)
        self._redis: redis.Redis | None = None

    async def connect(self) -> None:
        self._redis = redis.from_url(self._redis_url, decode_responses=True)
        logger.info("EventBus connected to Redis")

    async def publish(self, event: Event) -> bool:
        """Publish `event`. Never raises; returns whether a subscriber got
        it, so a caller whose alert must arrive can retry. Pub/sub drops
        a message nobody is subscribed to — the Telegram notifier while it
        re-subscribes after Redis came back, say — so Redis accepting the
        PUBLISH is not enough."""
        if self._redis is None:
            logger.warning("EventBus not connected, skipping event: %s", event.type)
            return False
        try:
            event.mode = self._mode
            receivers = await self._redis.publish(self._channel, event.to_json())
        except Exception:
            logger.exception("Failed to publish event: %s", event.type)
            return False
        logger.debug("Published event: %s (%s receivers)", event.type, receivers)
        return bool(receivers)

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()


class NoOpEventBus(EventBus):
    """Event bus that does nothing — used when Redis is unavailable."""

    async def connect(self) -> None:
        logger.info("NoOpEventBus: Redis disabled")

    async def publish(self, event: Event) -> bool:
        # Nowhere to deliver to, now or later — retrying would not help.
        logger.debug("NoOpEventBus: %s", event.type)
        return True

    async def close(self) -> None:
        pass
