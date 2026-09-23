"""Per-tenant cap on concurrently enabled strategies, applied at boot.

`tenants.max_active_strategies` is operator-set. The dashboard enforces
it where a tenant turns a strategy ON (`/api/control/strategy/[name]/
toggle`), but a freshly spawned bot starts with every allowlisted
strategy enabled — so a tenant capped at 3 ran all of them until they
touched a switch. The orchestrator now injects the cap as
`TENANT_MAX_ACTIVE_STRATEGIES`, and this module trims the bot to it
before the first tick.

"Enabled" means what the toggle route counts: instantiated (after the
mainnet and tenant allowlists) and not in the mode's Redis `disabled`
set. Surplus strategies are added to that set, so the tenant sees them
as switched off in the dashboard and can choose which ones to swap in,
within the cap. The first N in registry order are the ones kept, so two
boots from the same state keep the same N.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from hypertrade.events.types import ErrorOccurred

if TYPE_CHECKING:
    from hypertrade.engine.control import BotControl
    from hypertrade.events.bus import EventBus

logger = logging.getLogger("hypertrade")

_EVENT_STRATEGY = "strategy-cap"


class MalformedCapError(ValueError):
    """TENANT_MAX_ACTIVE_STRATEGIES is set but is not a non-negative
    integer."""


def parse_strategy_cap(raw: str | None) -> int | None:
    """Parse the TENANT_MAX_ACTIVE_STRATEGIES env value.

    Returns None when unset or blank (no cap — the NULL semantics of
    `tenants.max_active_strategies`), otherwise the cap. `0` is a real
    cap: no strategy may be enabled.

    Raises MalformedCapError for anything that is not plain ASCII
    digits. `int()` alone would accept `-1`, `+3`, `1_000` and
    non-ASCII digits, none of which the orchestrator ever writes.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"[0-9]+", text):
        raise MalformedCapError("expected a non-negative integer")
    return int(text)


async def _alert(event_bus: EventBus | None, message: str) -> None:
    # Best effort, like every event on the bus: the WARNING/ERROR log
    # lines are the durable record. A boot-time publish can also beat
    # the Telegram owner's own subscription.
    if event_bus is None:
        return
    try:
        await event_bus.publish(
            ErrorOccurred(strategy=_EVENT_STRATEGY, message=message),
        )
    except Exception:
        logger.exception("strategy-cap: event publish failed")


async def enforce_strategy_cap(
    names: list[str],
    raw_cap: str | None,
    control: BotControl | None,
    event_bus: EventBus | None,
    mode: str,
) -> list[str]:
    """Apply the tenant's strategy cap. Returns the names to instantiate.

    - No cap: `names` unchanged, Redis untouched.
    - Cap not exceeded: `names` unchanged.
    - Cap exceeded: every enabled strategy past the first `cap` (in
      `names` order, i.e. registry order) is added to the Redis
      `disabled` set, each logged at WARNING, and ONE ErrorOccurred
      names them all. `names` is returned unchanged — the disabled
      ones stay instantiated and visible, exactly as if the tenant had
      switched them off.

    Fails CLOSED, because this is a containment control:

    - Malformed value: logged, and no strategy is instantiated. Not
      written to Redis, so fixing the value and restarting restores the
      tenant's own switch positions.
    - No Redis (control is None, or the read/write fails): the disabled
      set can't be consulted or updated, and the runner would then run
      everything it was given. Only the first `cap` are instantiated.
    """
    try:
        cap = parse_strategy_cap(raw_cap)
    except MalformedCapError:
        logger.error(
            "TENANT_MAX_ACTIVE_STRATEGIES is malformed (%r) — failing CLOSED: "
            "no strategies will run. Fix the value and restart.",
            (raw_cap or "")[:40],
        )
        await _alert(
            event_bus,
            f"Strategy cap ({mode}): TENANT_MAX_ACTIVE_STRATEGIES is "
            f"malformed, so no strategies were started. Fix the tenant "
            f"limit and restart the bot.",
        )
        return []
    if cap is None:
        return list(names)

    if control is None:
        kept = list(names[:cap])
        logger.warning(
            "Strategy cap %d: Redis control unavailable, so the disabled set "
            "can't be applied — instantiating only the first %d: %s",
            cap, len(kept), kept,
        )
        return kept

    try:
        disabled = await control.get_disabled_strategies()
    except Exception:
        logger.exception(
            "Strategy cap %d: could not read the disabled set — instantiating "
            "only the first %d strategies",
            cap, cap,
        )
        return list(names[:cap])

    enabled = [n for n in names if n not in disabled]
    if len(enabled) <= cap:
        logger.info(
            "Strategy cap %d: %d enabled, within cap", cap, len(enabled),
        )
        return list(names)

    kept, surplus = enabled[:cap], enabled[cap:]
    failed = False
    for name in surplus:
        try:
            await control.disable_strategy(name)
        except Exception:
            logger.exception(
                "Strategy cap %d: failed to disable %s", cap, name,
            )
            failed = True
            continue
        logger.warning(
            "Strategy cap %d: disabled %s at boot (tenant limit; %d enabled "
            "before trimming)",
            cap, name, len(enabled),
        )

    await _alert(
        event_bus,
        f"Strategy cap ({mode}): tenant limit is {cap} enabled "
        f"strateg{'y' if cap == 1 else 'ies'}; disabled {len(surplus)} at "
        f"boot: {', '.join(surplus)}. Still enabled: "
        f"{', '.join(kept) if kept else 'none'}.",
    )

    if failed:
        # Some surplus strategy is still enabled in Redis, and the
        # runner reads that set every tick. Don't instantiate it.
        logger.warning(
            "Strategy cap %d: not every surplus strategy could be disabled — "
            "instantiating only the %d kept: %s",
            cap, len(kept), kept,
        )
        return list(kept)
    return list(names)
