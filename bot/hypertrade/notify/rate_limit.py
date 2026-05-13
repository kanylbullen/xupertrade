"""Async Redis-backed fixed-window rate limiter (M-1 fix).

Mirrors `dashboard/src/lib/rate-limit.ts`: atomic INCR + EXPIRE NX
in one pipeline, then read TTL so callers can tell the user how
long until the window rolls.

Used by the Telegram /link handler to throttle per-chat brute-force
attempts. The previous global-cooldown (5s, command-keyed) bucket
was per-command-name, not per-chat-id, so a single attacker spamming
/link drowned out legitimate /link from real tenants while only
being throttled themselves to ~1/5s — still 17k attempts/day against
a 10^6 codespace. Now each chat gets its own bucket, and the
codespace is 32^10 (see dashboard route).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import redis.asyncio as redis

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_in_seconds: int


async def check_rate_limit(
    client: redis.Redis,
    scope: str,
    bucket: str,
    *,
    max_events: int,
    window_seconds: int,
) -> RateLimitResult:
    """Increment the counter for (scope, bucket) and return whether
    the action is allowed.

    Fail-open: any Redis error is logged and treated as "allowed"
    so a Redis outage doesn't lock real users out of /link. The
    32^10 codespace is the primary defence; rate-limit is
    defence-in-depth.
    """
    key = f"ratelimit:{scope}:{bucket}"
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        # nx=True → only set TTL on first hit of the window so a
        # flood doesn't keep extending the window and starve the
        # client forever.
        pipe.expire(key, window_seconds, nx=True)
        pipe.ttl(key)
        results = await pipe.execute()
    except Exception:
        logger.exception("rate-limit pipeline failed; failing open for %s", key)
        return RateLimitResult(
            allowed=True, remaining=max_events, reset_in_seconds=window_seconds
        )

    try:
        count = int(results[0])
    except (TypeError, ValueError, IndexError):
        logger.warning("rate-limit: unexpected INCR result %r; failing open", results)
        return RateLimitResult(
            allowed=True, remaining=max_events, reset_in_seconds=window_seconds
        )

    try:
        raw_ttl = int(results[2])
    except (TypeError, ValueError, IndexError):
        raw_ttl = window_seconds
    # TTL can be -1 (no expiry — defence) or -2 (vanished between
    # INCR and TTL). Either is meaningless to the user; clamp to
    # window so the "try again in N min" message stays sane.
    ttl = raw_ttl if raw_ttl > 0 else window_seconds

    remaining = max(0, max_events - count)
    if count > max_events:
        return RateLimitResult(
            allowed=False, remaining=0, reset_in_seconds=ttl
        )
    return RateLimitResult(
        allowed=True, remaining=remaining, reset_in_seconds=ttl
    )
