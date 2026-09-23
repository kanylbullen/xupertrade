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
within the cap.

A strategy that holds an open position is NEVER trimmed. The tick loop
skips disabled strategies entirely and there are no exchange-side stop
orders, so disabling (or not instantiating) a strategy that is in a
trade leaves that position with no SL, no TP and no exit. Such
strategies are always instantiated, keep whatever switch position they
already had, and count toward the cap first; only flat strategies are
trimmed. When the open positions can't be read, nothing is trimmed.

Which flat strategies are kept, in order:
  1. on mainnet, those in the tenant's mainnet opt-in set (the runner
     only trades those, so keeping a non-opted-in one while disabling
     an opted-in one would invert the tenant's choice);
  2. the rest, in registry order — so two boots from the same state
     keep the same ones.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from hypertrade.events.types import ErrorOccurred

if TYPE_CHECKING:
    from hypertrade.db.repo import Repository
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
    cap: no flat strategy may be enabled.

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
    # Best effort, like every event on the bus: the log lines are the
    # durable record. A boot-time publish can also beat the Telegram
    # owner's own subscription.
    if event_bus is None:
        return
    try:
        await event_bus.publish(
            ErrorOccurred(strategy=_EVENT_STRATEGY, message=message),
        )
    except Exception:
        logger.exception("strategy-cap: event publish failed")


async def _strategies_holding_positions(
    names: list[str], repo: Repository | None,
) -> set[str] | None:
    """Names (within `names`) with an open DB position in this mode, or
    None when that can't be determined."""
    if repo is None:
        return None
    try:
        rows = await repo.get_open_positions()
    except Exception:
        logger.exception("strategy-cap: could not read open positions")
        return None
    wanted = set(names)
    return {r.strategy_name for r in rows if r.strategy_name in wanted}


def _holding_plus_first_flat(
    names: list[str], holding: set[str], cap: int,
) -> list[str]:
    """Every strategy holding a position, plus the first flat ones in
    registry order up to what the cap has left. Registry order kept."""
    room = max(0, cap - len(holding))
    flat_kept = set([n for n in names if n not in holding][:room])
    return [n for n in names if n in holding or n in flat_kept]


async def enforce_strategy_cap(
    names: list[str],
    raw_cap: str | None,
    control: BotControl | None,
    event_bus: EventBus | None,
    repo: Repository | None,
    mode: str,
    mainnet_tenant_id: str | None = None,
) -> list[str]:
    """Apply the tenant's strategy cap. Returns the names to instantiate.

    `mainnet_tenant_id` is set only on mainnet (and only when the bot
    knows its tenant) — exactly when the runner applies the tenant's
    mainnet opt-in set.

    - No cap: `names` unchanged; nothing read, nothing written.
    - Open positions unreadable (no repo, or the query fails): nothing
      is trimmed, whatever the cap says. One ERROR log and one
      ErrorOccurred. Trimming blind could orphan a live position.
    - Cap not exceeded: `names` unchanged.
    - Cap exceeded: flat enabled strategies past the kept ones (see the
      module docstring for the order) are added to the Redis `disabled`
      set, each logged at WARNING, and ONE ErrorOccurred names them
      all. `names` is returned unchanged — the disabled ones stay
      instantiated and visible, exactly as if the tenant had switched
      them off.

    Fails CLOSED for flat strategies, never for held positions:

    - Malformed value: only strategies holding a position are
      instantiated. Nothing is written to Redis, so fixing the value
      and restarting restores the tenant's own switch positions.
    - No Redis (control is None, or the disabled/opt-in read fails):
      the disabled set can't be consulted or updated, and the runner
      would run everything it was given. Holding strategies plus the
      first flat ones up to the cap are instantiated — except on
      mainnet, where the opt-in set decides which flat ones may trade
      and can't be read, so only the holding ones are.
    - A disable write fails: that strategy is not instantiated (it is
      flat by construction, so nothing is orphaned).
    """
    text = (raw_cap or "").strip()
    if not text:
        return list(names)

    try:
        cap: int | None = parse_strategy_cap(text)
    except MalformedCapError:
        cap = None

    holding = await _strategies_holding_positions(names, repo)
    if holding is None:
        logger.error(
            "Strategy cap: open positions could not be read — NOT trimming "
            "any strategy, so none can be left holding an unmanaged "
            "position. All %d stay as they are.",
            len(names),
        )
        await _alert(
            event_bus,
            f"Strategy cap ({mode}): open positions could not be read at "
            f"boot, so the tenant's strategy limit was NOT applied. Restart "
            f"the bot once the database is reachable.",
        )
        return list(names)

    if cap is None:
        kept = [n for n in names if n in holding]
        logger.error(
            "TENANT_MAX_ACTIVE_STRATEGIES is malformed (%r) — failing CLOSED: "
            "only strategies holding an open position will run: %s. Fix the "
            "value and restart.",
            text[:40], kept,
        )
        await _alert(
            event_bus,
            f"Strategy cap ({mode}): TENANT_MAX_ACTIVE_STRATEGIES is "
            f"malformed, so only strategies holding a position were "
            f"started ({', '.join(kept) if kept else 'none'}). Fix the "
            f"tenant limit and restart the bot.",
        )
        return kept

    async def redis_unavailable(why: str) -> list[str]:
        if mainnet_tenant_id:
            kept = [n for n in names if n in holding]
        else:
            kept = _holding_plus_first_flat(names, holding, cap)
        logger.warning(
            "Strategy cap %d: %s — instantiating only %s",
            cap, why, kept,
        )
        await _alert(
            event_bus,
            f"Strategy cap ({mode}): {why}, so only "
            f"{', '.join(kept) if kept else 'no strategies'} were started. "
            f"Restart the bot once Redis is reachable.",
        )
        return kept

    if control is None:
        return await redis_unavailable("Redis control unavailable")
    try:
        disabled = await control.get_disabled_strategies()
        opted_in = (
            await control.get_mainnet_enabled_strategies_for_tenant(
                mainnet_tenant_id,
            )
            if mainnet_tenant_id
            else None
        )
    except Exception:
        logger.exception("strategy-cap: Redis read failed")
        return await redis_unavailable("the strategy switches could not be read")

    enabled = [n for n in names if n not in disabled]
    held_enabled = [n for n in enabled if n in holding]

    def priority(n: str) -> int:
        if n in holding:
            return 0
        if opted_in is None or n in opted_in:
            return 1
        return 2

    # Stable sort: within a priority, registry order.
    ordered = sorted(enabled, key=priority)
    limit = max(cap, len(held_enabled))
    kept, surplus = ordered[:limit], ordered[limit:]
    if not surplus:
        logger.info(
            "Strategy cap %d: %d enabled, within cap", cap, len(enabled),
        )
        return list(names)
    if len(held_enabled) > cap:
        logger.warning(
            "Strategy cap %d: %d strategies hold open positions and are all "
            "kept enabled anyway: %s",
            cap, len(held_enabled), held_enabled,
        )

    failed: set[str] = set()
    for name in surplus:
        try:
            await control.disable_strategy(name)
        except Exception:
            logger.exception(
                "Strategy cap %d: failed to disable %s", cap, name,
            )
            failed.add(name)
            continue
        logger.warning(
            "Strategy cap %d: disabled %s at boot (tenant limit; %d enabled "
            "before trimming)",
            cap, name, len(enabled),
        )

    kept_note = ", ".join(kept) if kept else "none"
    if held_enabled:
        kept_note += f" (holding positions: {', '.join(held_enabled)})"
    await _alert(
        event_bus,
        f"Strategy cap ({mode}): tenant limit is {cap} enabled "
        f"strateg{'y' if cap == 1 else 'ies'}; disabled {len(surplus)} flat "
        f"one(s) at boot: {', '.join(surplus)}. Still enabled: {kept_note}.",
    )

    if failed:
        # Still enabled in Redis, and the runner reads that set every
        # tick. Don't instantiate them. They are flat by construction.
        logger.warning(
            "Strategy cap %d: could not disable %s — not instantiating them",
            cap, sorted(failed),
        )
        return [n for n in names if n not in failed]
    return list(names)
