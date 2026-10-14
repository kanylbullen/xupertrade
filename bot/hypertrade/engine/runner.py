"""Strategy engine runner — the core loop."""

import asyncio
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp

from hypertrade.config import settings
from hypertrade.data.feed import fetch_candles
from hypertrade.db.repo import ReconcileResult, Repository, split_pending_close
from hypertrade.engine import funding
from hypertrade.engine.control import BotControl
from hypertrade.engine.portfolio import PortfolioManager
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.engine.strategy_allowlist import filter_strategies_for_tick
from hypertrade.events.bus import EventBus
from hypertrade.events.types import (
    BotHeartbeat,
    ErrorOccurred,
    HodlVerdictChanged,
    LogEntry,
    SignalGenerated,
    TickCompleted,
    TradeExecuted,
)
from hypertrade.exchange.base import Exchange, ExchangeReadError, OrderType
from hypertrade.reconcile.fills import realized_pnl
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import get_strategy_family

logger = logging.getLogger(__name__)


class StartupRefused(RuntimeError):
    """A live-mode (testnet/mainnet) boot precondition failed and the bot
    cannot make itself safe any other way. `main.run()` turns this into
    exit status 1 so the container's `unless-stopped` restart policy
    retries the boot, instead of the process trading without the guards
    the precondition exists for.
    """


@dataclass
class FlatAllStatus:
    """Outcome of one flat-all attempt.

    Exists so the caller can tell "the book is flat" from "I never got
    to look at the book". The second one must not acknowledge the
    operator's request (`bot/reports/analysis-2026-09-15.md` § 4, High).
    """

    ok: bool
    closed: int = 0
    failed: int = 0
    reason: str = ""


class TradeDbDivergence(Exception):
    """Raised when an order has been placed on the exchange but the
    matching DB write failed. The handler in `_execute_signal` already
    logged details, paused the bot, and published an ErrorOccurred
    event — `_run_strategy`'s catch-all should detect this type and
    skip its generic "Strategy tick failed" handling to avoid
    double-logging / double-publishing the same incident.
    """


# Errors that indicate transient HL/network issues — bot recovers
# automatically on next tick, so we don't want to spam Telegram with
# a separate alert per strategy per failed tick (22 strategies × 1/min
# = 22 events/min during a HL outage). The 2026-05-09 4.5h outage
# would have produced ~6000 events without this filter.
_TRANSIENT_NETWORK_ERRORS = (
    asyncio.TimeoutError,
    ConnectionError,
    socket.gaierror,
    aiohttp.ClientError,
)

# HTTP statuses from the HL SDK that clear on their own.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 502, 503, 504})


def _is_transient_network_error(exc: BaseException) -> bool:
    """True for HL/network outages where the bot recovers on its own."""
    # An ExchangeReadError is a wrapper, never the diagnosis — look at
    # what it wraps. Without this, every read that now raises instead of
    # returning `[]` would be classed non-transient and publish one
    # Telegram error per strategy per tick for the whole outage, which
    # is exactly the noise the outage aggregator exists to prevent.
    if isinstance(exc, ExchangeReadError) and exc.__cause__ is not None:
        return _is_transient_network_error(exc.__cause__)
    if isinstance(exc, _TRANSIENT_NETWORK_ERRORS):
        return True
    # The HL SDK raises `ServerError` for >= 500 and `ClientError` for
    # 4xx, both carrying `.status_code`. Only gateway/unavailable 5xx and
    # request-timeout / rate-limit clear on their own. Read the status
    # instead of hunting for digits in the message: the message is the
    # response body, and a 500 whose body mentions "502" is still a 500
    # (same predicate as the exchange wrapper's read retry).
    try:
        from hyperliquid.utils.error import ClientError, ServerError  # noqa: PLC0415
        if isinstance(exc, (ServerError, ClientError)):
            return getattr(exc, "status_code", None) in _TRANSIENT_HTTP_STATUSES
    except ImportError:  # SDK not installed (unlikely in prod, possible in tests)
        pass
    # Bare `requests.exceptions.ConnectionError` doesn't subclass our
    # ConnectionError above (different base); name-match as fallback.
    name = type(exc).__name__
    if name in ("ConnectionError", "ConnectTimeout", "ReadTimeout"):
        return True
    return False


_TRANSIENT_VERDICT_SUBSTRINGS = (
    "evaluation failed",
    "no data",
)


def _is_transient_unknown_verdict(verdict: str | None) -> bool:
    """Return True if `verdict` looks like a transient sentinel produced
    by hodl.base.Signal.evaluate() when an upstream fetch raised or
    returned no data. Used to suppress recovery-noise notifications:
    if we never pinged the user about the failure, we shouldn't ping
    them about the recovery either.

    Copilot review fix on PR #98: previous predicate was
    `startswith("unknown")` which would also suppress legitimate
    Unknown-shaped verdicts like "Unknown — manual disabled" or
    "Unknown — unsupported asset". Narrow to the specific transient
    sentinels emitted by the fetch-failure / no-data paths.
    """
    if not verdict:
        return False
    v = verdict.lower()
    return any(s in v for s in _TRANSIENT_VERDICT_SUBSTRINGS)


_start_time = time.time()

# NU-2: how often a standing hold is re-announced, how long opens may wait
# for a reconcile pass that counts before that is, and the reasons shown.
HOLD_REALERT_SECONDS = 1800.0
OPENS_STUCK_SECONDS = 600.0
_HOLD_SET = "reconcile hold is set"
_FIRST_PASS = "first reconcile pass since the bot started"
_AWAIT_FIRST_PASS = "waiting for the first reconcile pass since the bot started"
_AWAIT_PASS_AFTER_CLEAR = "waiting for a reconcile pass after the hold cleared"

# NU-5a: after this many attempts in a row that left a close's row open
# (rejected, filled zero or short) on one (strategy, coin), every further
# attempt gets the wider IOC band (the HL SDK's own `market_close`
# default); the first of those that misses too alerts critical.
CLOSE_REJECTS_BEFORE_WIDE_BAND = 3
WIDE_CLOSE_BAND = 0.05


@dataclass
class _PendingClose:
    """NU-5a: an exit this bot sent that left its row open — rejected,
    filled zero or short, or raised before its outcome was known.

    From then on the close is the engine's, not the strategy's: it goes
    out every tick, whatever the strategy's rule says by then, until it
    fills, the row is booked as closed outside the bot, the strategy is
    reset to flat, or the row is gone. Pine's `strategy.close` fills on the
    next bar whether or not its condition still holds; an edge-triggered
    exit (a crossunder, a Supertrend flip, a time of day) fires on one bar
    only, and used to be dropped when that bar ended. The row carries a
    marker (`repo.PENDING_CLOSE_KEY`), so a restart sends it again.
    """

    side: str  # the row's side
    reason: str  # the exit's own reason, for its log lines and trade row
    misses: int = 0  # attempts in a row that left the row open
    alerted: int = 0  # highest alert delivered: 1, or 2 the critical
    logged: int = 0  # highest alert logged at ERROR; redelivery logs at INFO


@dataclass
class _TickControls:
    """The control flags one tick runs under (`_read_tick_controls`)."""

    paused: bool = False
    # None: unreadable, so only strategies holding a position run.
    disabled: set[str] | None = field(default_factory=set)
    # None outside mainnet: no allowlist layer.
    mainnet_enabled: set[str] | None = None
    allowlist_unreadable: bool = False
    # What could not be read, as reasons for this tick's opens hold.
    unreadable: list[str] = field(default_factory=list)


def _held_on_side(pos, side: str) -> float:
    """How much of a `side` position the exchange's `pos` is: its size
    on that side, 0.0 when flat or on the other side."""
    return float(pos.size) if pos is not None and pos.side == side else 0.0


def _exchange_text(pos) -> str:
    return f"{pos.side} {pos.size}" if pos is not None else "flat"


def _closes_side(signal: Signal | None, side: str) -> bool:
    """Whether `signal` itself closes a `side` position: its exit, or a
    flip, whose close half goes first."""
    if signal is None:
        return False
    if side == "long":
        return signal.action in (SignalAction.CLOSE_LONG, SignalAction.OPEN_SHORT)
    return signal.action in (SignalAction.CLOSE_SHORT, SignalAction.OPEN_LONG)


def _holds(strategy) -> bool:
    try:
        return bool(strategy.holds_position())
    except Exception:
        return False


class EngineRunner:
    # How long a refusal note stands when the caller gave no bar time.
    _REFUSAL_NOTE_TTL_SECONDS = 3600.0
    # NU-5a: a read that finds a close's position gone is confirmed by a
    # second read this much later (reconcile's rule: a read can miss a coin).
    CLOSE_CONFIRM_DELAY_SECONDS = 2.0

    def __init__(
        self,
        exchange: Exchange,
        strategies: list[Strategy],
        repo: Repository | None = None,
        event_bus: EventBus | None = None,
        control: BotControl | None = None,
        pushed_leverage: dict[str, int] | None = None,
    ) -> None:
        self.exchange = exchange
        self.strategies = strategies
        self.repo = repo
        self.event_bus = event_bus
        self.control = control
        self.portfolio = PortfolioManager(
            exchange, control=control, event_bus=event_bus,
        )
        self._last_reconcile = 0.0  # epoch seconds
        # Last reconcile summary we sent to Telegram. A divergence
        # reconcile cannot resolve (an unpriceable row, a multi-hour HL
        # outage) repeats verbatim every 5 minutes; publishing it every
        # time is the "Telegram is for humans" failure the fetch-outage
        # aggregator exists to avoid. Identical consecutive summaries
        # are logged but published once.
        self._last_reconcile_notice: str | None = None
        self._last_funding_poll = 0.0
        self._last_hodl_check = 0.0
        self._last_hodl_zones: dict[str, str] = {}  # signal_name -> last verdict
        self._last_vault_poll = 0.0
        self._vault_poller = None  # lazy-init on first use
        self._last_rate_check = 0.0
        self._rate_alarm_paused: set[str] = set()  # strategies auto-paused this run
        # Audit H1 (2026-05-10): track the leverage we last pushed per
        # coin so we can re-push BEFORE an OPEN if a runtime override
        # (Redis HSET, dashboard endpoint) bumped a strategy's `s.leverage`
        # since startup. Without this, the bot's notional calc uses the
        # new leverage but HL still has the startup leverage → margin
        # used = `notional / startup_leverage` instead of
        # `notional / new_leverage`. On a 10× bump that's 10× the
        # expected margin → liquidation path.
        # Seeded with what `main.py`'s boot-time push actually got
        # accepted, so "last successfully pushed" includes it.
        self._pushed_leverage: dict[str, int] = dict(pushed_leverage or {})
        # symbol -> target leverage we already told Telegram we could not
        # push. One ErrorOccurred per (symbol, target) failure episode;
        # cleared by the next successful push for that symbol.
        self._leverage_push_alerted: dict[str, int] = {}
        # (strategy, symbol) -> (category, bar time, monotonic time) of the
        # last refusal logged/published. See `_note_refusal`.
        self._refusal_notes: dict[tuple[str, str], tuple[str, object, float]] = {}
        # True while strategies do not know about the DB's open positions
        # (the startup read failed). No strategy runs until a restore
        # succeeds — see `_halt_for_unrestored_state`.
        self._restore_pending = False
        # Backoff between attempts of the startup open-positions read.
        # Instance attribute so tests can shrink it.
        self._restore_read_backoff: tuple[float, ...] = (1.0, 2.0, 4.0)
        # Transient fetch-failure outage window (CLAUDE.md backlog
        # Open-Low: "Suppress Telegram noise on transient HL-fetch
        # failures"). Publishing per strategy per tick spammed Telegram
        # at ~22 events/min during the 2026-05-09 HL outage; fully
        # suppressing instead left a multi-hour outage Telegram-silent.
        # Now per-tick failures accumulate into an outage window:
        #   - window clears before FETCH_OUTAGE_ALERT_SECONDS → no
        #     notification at all (bot auto-recovers, logs carry detail)
        #   - window persists ≥ threshold → exactly ONE ErrorOccurred
        #     per window summarizing the affected strategies
        #   - recovery closes the window log-only (one ping per window)
        # `_tick_fetch_failures` is the per-tick scratch set filled by
        # _run_strategy / the tick catch-all and drained by
        # _settle_fetch_outage() after each strategy loop.
        self._tick_fetch_failures: set[str] = set()
        self._outage_start: float | None = None  # first failure of the open window
        self._outage_affected: set[str] = set()  # strategies failed during the window
        self._outage_alerted: bool = False  # one-alert-per-window latch
        # NU-2 restore guards. One lost-state alert per episode, queued
        # until a subscriber has it, even after the sentinel is written.
        self._lost_state_alerted = False
        self._lost_state_alert: str | None = None
        self._lost_state_at = ""
        self._lost_state_effects = ""
        # Monotonic time of the last hold alert; None when nothing is held.
        self._hold_alert_at: float | None = None
        # Pass 2 holds every orphan until one readable, unpaused pass ran:
        # a restore, a key change or an emptied DB shows up there first.
        # Opens wait for that pass (`_awaiting`: which one, since when; a
        # paused pass restarts the clock; no repo, no pass, no wait), and
        # for the first one after a hold or an unreadable state cleared:
        # an open that nets into an orphan gives its coin a row, and the
        # orphan is then never held, only logged as a size mismatch.
        self._awaiting = _AWAIT_FIRST_PASS if repo is not None else None
        self._awaiting_since = time.monotonic()
        # The last `_check_control_state` answer, to see a hold clear.
        self._control_hold: str | None = None
        # NU-5a: (strategy, coin) -> the close the engine owes its open
        # row (`_PendingClose`): sent every tick until it fills, the row is
        # booked as closed outside the bot, the strategy is reset to flat,
        # or the row is gone.
        self._pending_closes: dict[tuple[str, str], _PendingClose] = {}
        # NU-5a: a key for each strategy that held a position before this
        # tick's `on_candle`, with its `export_state()` dict (None if it
        # exports none); `_resync_strategy_to_row` re-arms with it.
        self._pre_signal_state: dict[str, dict | None] = {}
        # NU-5a: a flip's (strategy, coin) while its close is in flight ->
        # how `_close_left_open` settled that close, if it ran.
        self._flip_closes: dict[tuple[str, str], str | None] = {}
        # Why opens are held for this tick only (a flat-all request, a
        # control read that failed), on top of the NU-2 hold; exits run.
        self._tick_hold: str | None = None
        # (request token, reason) of the last "Flat-all did not complete"
        # delivered: once per request and reason, not once per tick.
        self._flat_all_notice: tuple[str, str] | None = None

    async def startup(self) -> None:
        """Restore in-memory strategy state from DB after a restart.

        Also owns the startup reconcile. It used to run in `main.py`
        before the runner existed, which meant it had no pause gate, no
        PnL callback and no event bus: a boot during an HL blip flattened
        the book silently, even on a paused bot. Here it goes through
        `_run_reconcile`, so it gets all three. It runs BEFORE state
        restoration, exactly as the `main.py` call did, so a restored
        strategy is never restored into a row reconcile is about to
        close.

        NOTE: PR 4c removed `_kick_caddy_tls_restore` (the dashboard now
        owns Caddy admin via `dashboard/src/lib/caddy-admin.ts`). The
        callers in this method were accidentally left behind and crashed
        every tenant-bot at startup with `AttributeError`. Removed in
        a follow-up hotfix.
        """
        if not self.repo:
            return

        try:
            await self._run_reconcile("Startup")
        except Exception:
            logger.exception("Startup reconcile failed (continuing without it)")

        if not await self._restore_from_db(self._restore_read_backoff):
            await self._halt_for_unrestored_state()

    async def _restore_from_db(self, backoff: tuple[float, ...]) -> bool:
        """Read the open positions and restore every strategy from them.

        Retries the read once per entry in `backoff` (seconds to wait
        before each retry). Returns False only when the read never
        succeeded — the caller then decides how to stay safe; it used to
        just `return`, leaving every strategy flat in memory while the
        exchange held its positions (`analysis-2026-09-15.md` § 4, Low).
        """
        delays = list(backoff)
        attempt = 0
        while True:
            attempt += 1
            try:
                positions = await self.repo.get_open_positions()
                break
            except Exception:
                if not delays:
                    logger.exception(
                        "State restore: reading open positions failed "
                        "(attempt %d, giving up)", attempt,
                    )
                    return False
                delay = delays.pop(0)
                logger.warning(
                    "State restore: reading open positions failed "
                    "(attempt %d) — retrying in %.0fs", attempt, delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
        await self._restore_strategies(positions)
        return True

    async def _halt_for_unrestored_state(self) -> None:
        """The open-positions read failed for good: stop trading.

        Paper keeps the old behaviour (log and continue flat) — nothing
        real is at risk. On testnet/mainnet the strategies do not know
        about positions the exchange may hold, so running them means
        trading next to unmanaged positions (no SL, duplicate opens). The
        bot pauses via BotControl, tells Telegram, and retries the restore
        on the first tick it finds un-paused; if that retry fails too it
        pauses again. Without BotControl there is no pause to set, so
        the boot is refused and the container restarts.
        """
        if settings.is_paper:
            logger.error(
                "State restore failed — PAPER mode, continuing with every "
                "strategy flat in memory",
            )
            return

        self._restore_pending = True
        if self.control is None:
            raise StartupRefused(
                "could not read open positions for state restoration and "
                "there is no BotControl to pause the bot with"
            )
        try:
            await self.control.set_paused(True)
        except Exception as e:
            raise StartupRefused(
                "could not read open positions for state restoration, and "
                f"pausing the bot failed too ({type(e).__name__}: {e})"
            ) from e
        message = (
            "Could not read open positions from the database to restore "
            "strategy state. Strategies would trade without knowing about "
            "open positions, so the bot is PAUSED. Fix the database, then "
            "un-pause: the restore is retried before any strategy runs, "
            "and the bot pauses again if it still fails."
        )
        logger.error("State restore failed — %s", message)
        await self._publish_error("state-restore", message)

    async def _retry_pending_restore(self) -> bool:
        """One restore attempt on an un-paused tick while a restore is
        pending. True → strategies may run. False → re-paused."""
        if await self._restore_from_db(()):
            self._restore_pending = False
            logger.warning(
                "State restore succeeded after the operator un-paused — "
                "strategies resume",
            )
            return True
        if self.control is not None:
            try:
                await self.control.set_paused(True)
            except Exception:
                logger.exception("State restore: re-pausing the bot failed")
        message = (
            "Bot was un-paused but open positions still cannot be read "
            "from the database; paused again. No strategy has run."
        )
        logger.error("State restore failed — %s", message)
        await self._publish_error("state-restore", message)
        return False

    async def _publish_error(self, strategy: str, message: str) -> bool:
        """Publish an ErrorOccurred; a publish failure is logged, never
        raised. False only when a bus exists and says nobody got it, so a
        caller whose alert must arrive can retry."""
        if not self.event_bus:
            return True
        try:
            delivered = await self.event_bus.publish(
                ErrorOccurred(strategy=strategy, message=message)
            )
        except Exception:
            logger.exception("%s: event publish failed", strategy)
            return False
        return delivered is not False

    def _restore_strategy_from_row(self, strat: Strategy, pos) -> None:
        """Restore one strategy's in-memory position state from its DB row.

        Prefers the exact `state_json` captured at open time; falls back
        to recompute-based `restore_state` when it was never persisted
        (legacy rows or strategies that don't override export_state).
        """
        state = None
        if pos.state_json:
            try:
                state = json.loads(pos.state_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "[%s] Invalid state_json — falling back to recompute",
                    pos.strategy_name,
                )
        # The engine's pending-close marker is not the strategy's state.
        state, _ = split_pending_close(state)
        if state is not None:
            # Defensive sanity check on the persisted dict shape
            # (audit L7). Strategies' restore_from_json silently
            # falls back to defaults when expected keys are missing,
            # which makes silent state corruption hard to spot. We
            # also need an `isinstance(dict)` guard because
            # `state_json` can deserialize to non-dict values
            # (`null`, `[]`, `"foo"` from corrupted/hand-edited rows)
            # — `set(state.keys())` would AttributeError otherwise
            # and break startup (audit-bundle-4 review fix).
            if not isinstance(state, dict):
                logger.warning(
                    "[%s] state_json deserialized to non-dict %r — "
                    "falling back to recompute restore",
                    pos.strategy_name, type(state).__name__,
                )
                strat.restore_state(pos.side, pos.entry_price)
                return
            _expected = {"in_long", "in_short", "entry", "sl", "tp"}
            _missing = _expected - set(state.keys())
            if _missing and any(
                hasattr(strat, "_" + k.removeprefix("in_"))
                or hasattr(strat, "_" + k)
                for k in _missing
            ):
                logger.warning(
                    "[%s] state_json missing keys %s — fields will "
                    "use restore_from_json defaults (likely safe but "
                    "indicates schema drift)",
                    pos.strategy_name, sorted(_missing),
                )
            strat.restore_from_json(pos.side, pos.entry_price, state)
            logger.info(
                "Restored %s state from JSON: %s @ %.2f (state=%s)",
                pos.strategy_name, pos.side, pos.entry_price, state,
            )
        else:
            strat.restore_state(pos.side, pos.entry_price)
            logger.info(
                "Restored %s state (recomputed): %s @ %.2f",
                pos.strategy_name, pos.side, pos.entry_price,
            )

    @staticmethod
    def _pending_close_marker(pos) -> dict | None:
        """The engine's pending-close marker on an open row, or None."""
        try:
            state = json.loads(pos.state_json) if pos.state_json else None
        except (TypeError, ValueError):
            return None
        return split_pending_close(state)[1]

    async def _restore_strategies(self, positions: list) -> None:
        """Restore every strategy from the open DB rows (plus the Redis
        cooldown snapshot for strategies that are flat)."""
        strat_by_name = {s.name: s for s in self.strategies}
        restored = 0
        unmanaged: list[str] = []
        for pos in positions:
            strat = strat_by_name.get(pos.strategy_name)
            pending = self._pending_close_marker(pos)
            if strat is None:
                # The strategy is not running in this bot (allowlist,
                # mainnet opt-in, a strategy cap). Nothing will manage this
                # position: no SL, no exit signal — and no pending close.
                logger.warning(
                    "State restore: open %s %s row of %s has no running "
                    "strategy — the position is UNMANAGED%s",
                    pos.symbol, pos.side, pos.strategy_name,
                    " and its pending close is abandoned" if pending else "",
                )
                unmanaged.append(
                    f"{pos.strategy_name} {pos.symbol} {pos.side}"
                    + (" (its close was pending)" if pending else "")
                )
                continue
            self._restore_strategy_from_row(strat, pos)
            restored += 1
            if pending is not None:
                self._pending_closes[(pos.strategy_name, pos.symbol)] = (
                    _PendingClose(
                        side=pos.side,
                        reason=str(pending.get("reason") or "exit"),
                    )
                )
                logger.warning(
                    "[%s] a close of its %s %s %s was pending before the "
                    "restart (since %s) — sent again from the first tick",
                    pos.strategy_name, pos.side, pos.size, pos.symbol,
                    pending.get("since", "?"),
                )
        if unmanaged:
            await self._publish_error(
                "state-restore",
                f"{len(unmanaged)} open position(s) belong to strategies "
                f"that are not running in this bot, so nothing manages them "
                f"(no SL, no exit): {', '.join(sorted(unmanaged))}. Close "
                f"them or re-enable the strategy.",
            )

        # Audit M6 follow-up: also restore Redis-backed strategy state for
        # strategies that are FLAT but might be in cooldown. The
        # position-table state_json only covers in-position windows; the
        # Redis snapshot covers the post-close cooldown window. Without
        # this, a restart inside the cooldown window would reset the
        # counter and let the strategy immediately re-enter on the same
        # stale bar.
        #
        # A strategy with no open DB row is flat — the DB is the source of
        # truth — so only the snapshot's cooldown fields are restored,
        # never its position flags. The snapshot is written after OPENs
        # too, and a position closed outside the strategy's own signal
        # left it saying "in position": on 2026-09-23 seven of fifteen
        # testnet strategies restored as holding positions that had been
        # closed days earlier, and never traded again.
        if self.control:
            open_names = {p.strategy_name for p in positions}
            for strat in self.strategies:
                if strat.name in open_names:
                    continue  # already restored from DB above
                try:
                    redis_state = await self.control.load_strategy_state(strat.name)
                except Exception:
                    logger.exception(
                        "load_strategy_state failed for %s (skipping)", strat.name,
                    )
                    continue
                if not redis_state:
                    continue
                try:
                    forced_flat = strat.restore_cooldown_only(redis_state)
                except Exception:
                    logger.exception(
                        "restore_cooldown_only failed for %s redis state %s "
                        "(position state cleared, cooldown may be lost)",
                        strat.name, redis_state,
                    )
                    continue
                if not forced_flat:
                    logger.info(
                        "Restored %s cooldown state from Redis: %s",
                        strat.name, redis_state,
                    )
                    continue
                logger.info(
                    "%s: snapshot said in position but no open DB row — "
                    "starting flat (discarded snapshot: %s)",
                    strat.name, redis_state,
                )
                # Rewrite the snapshot so the next restart does not have
                # to discard it again.
                await self._save_strategy_snapshot(strat)

        if positions:
            logger.info(
                "State restoration complete: %d/%d positions restored",
                restored, len(positions),
            )

    async def _run_reconcile(self, label: str) -> "ReconcileResult | None":
        """Run one reconcile pass and make sure the outcome is visible.

        Two things the old call site got wrong, both from
        `bot/reports/analysis-2026-09-15.md` § 2:

        - Results only ever reached `logger.warning`, so Telegram never
          heard that the book had been flattened. Any action, failure or
          read-failure skip now publishes an `ErrorOccurred`.
        - Reconcile ran before the tick's `paused` check, so pausing the
          bot did not stop it closing rows, writing `Trade` rows, booking
          PnL and market-closing the very positions the pause was meant
          to preserve. A paused pass is now a DRY RUN: it reports what it
          would have closed and writes nothing. A pause flag we cannot
          read is treated AS paused.
        """
        if not self.repo:
            return None

        paused = False
        if self.control:
            try:
                paused = await self.control.is_paused()
            except Exception:
                logger.warning(
                    "Reconcile: could not read the paused flag — assuming "
                    "PAUSED, so this pass writes nothing and orders nothing",
                )
                paused = True
        if paused:
            self._awaiting_since = time.monotonic()  # paused is not stuck
            logger.info(
                "Reconcile: bot is paused — dry run. Divergences are "
                "reported; no rows closed, no trades written, no orders.",
            )

        hold = await self._check_control_state()
        traded = {s.symbol for s in self.strategies}
        first = _FIRST_PASS if self._awaiting == _AWAIT_FIRST_PASS else None
        result = await self.repo.reconcile_positions(
            self.exchange,
            close_exchange_orphans=not paused,
            on_strategy_close=self._on_reconcile_close,
            dry_run=paused,
            hold_orphans=hold or first,
            traded_symbols=traded,
        )
        if hold in (None, _HOLD_SET) and not (
            paused or result.skipped or result.unresolved_orphans
        ):
            # A pass that counts: opens no longer wait for one. Not one
            # that left an orphan unheld (its re-read or close failed).
            self._awaiting = None
            self.portfolio.opens_held = hold
        held = sorted(traded.intersection(result.held_orphans)) if hold is None else []
        if held:
            # Held on a coin we trade (several at once, or the first pass):
            # the hold keeps it held until a human clears it — not until a
            # count drops to one — and blocks opens that would net into it.
            await self._write_hold()
            hold = self.portfolio.opens_held = self._control_hold = _HOLD_SET
            self._last_reconcile_notice = None  # a re-set hold is news
            result.failures.append(
                f"Reconcile hold SET: {', '.join(held)} on the exchange with "
                "no DB row — this bot opens nothing and closes none of them "
                f"until a human clears the hold. {self._clear_hold_how()}"
            )

        if result.skipped:
            logger.warning("%s reconcile: %s", label, result.skipped)
        elif result.took_action:
            logger.warning(
                "%s reconcile: %d action(s), %d failure(s) — %s",
                label, len(result.actions), len(result.failures),
                result.summary(),
            )

        notice = result.summary() if (result.skipped or result.took_action) else None
        delivered = False
        if notice is None:
            self._last_reconcile_notice = None
        elif notice == self._last_reconcile_notice:
            logger.info(
                "Reconcile: same outcome as the previous pass — not "
                "re-publishing to Telegram (%s)", notice,
            )
        elif delivered := await self._publish_error(
            "reconcile", f"{label} reconcile — {notice}",
        ):
            # Only a delivered notice counts, so an undelivered one is
            # sent again next pass instead of being deduplicated away.
            self._last_reconcile_notice = notice
        # The notice announced the hold only if it carried HELD lines.
        await self._remind_hold(
            hold, result, delivered and bool(result.held_orphans),
        )
        return result

    async def _check_control_state(self) -> str | None:
        """NU-2, every tick and reconcile pass: why this bot may neither
        open nor market-close an exchange orphan, or None. Runs guard 1
        (a missing sentinel: Redis lost the control state), retries an
        unwritten hold or undelivered alert, fails closed and never
        raises, so exits keep running."""
        why = await self._read_control_state()
        if self._control_hold is not None and why is None:
            # Just cleared: opens wait for a pass that counts, and that
            # pass runs first in this tick, before any strategy runs.
            if self._awaiting is None and self.repo is not None:
                self._awaiting = _AWAIT_PASS_AFTER_CLEAR
                self._awaiting_since = time.monotonic()
            self._last_reconcile = 0.0
        self._control_hold = why
        # Without BotControl there is no hold to honour (not production).
        self.portfolio.opens_held = (
            (why or self._awaiting) if self.control is not None else None
        )
        if why is None and self._lost_state_alert is not None:
            # Cleared before anyone got the alert: the hold it names is
            # gone, what the lost state reset is not.
            self._lost_state_alert = (
                f"Redis control state was lost at {self._lost_state_at}; "
                "the reconcile hold has since been cleared by hand. "
                f"{self._lost_state_effects}"
            )
        if self._lost_state_alert is not None and await self._publish_error(
            "restore-guard", self._lost_state_alert,
        ):
            self._lost_state_alert = None
            self._hold_alert_at = time.monotonic()
        return why

    async def _reconcile_due(self) -> bool:
        """Every 5 minutes, and on every unpaused tick while opens wait
        for a pass that counts (`_awaiting`): a skipped pass is retried on
        the next tick; a paused bot keeps the 5-minute cadence."""
        if time.time() - self._last_reconcile > 300:
            return True
        if self.control is None or self._awaiting is None:
            return False
        try:
            return not await self.control.is_paused()
        except Exception:
            return False

    async def _read_control_state(self) -> str | None:
        if self.control is None:
            return "no Redis control connection"
        try:
            if not await self.control.sentinel_present():
                await self._secure_lost_control_state()
                return _HOLD_SET
            self._lost_state_alerted = False  # back, if a human clear wrote it
            if self.control.hold_unwritten and not await self._write_hold():
                return _HOLD_SET
            held = await self.control.is_reconcile_hold_active()
        except Exception:
            logger.warning(
                "Restore guard: control state unreadable — no opens and "
                "no exchange-orphan closes until it can be read",
                exc_info=True,
            )
            return "Redis control state unreadable"
        return _HOLD_SET if held else None

    async def _write_hold(self) -> bool:
        """Set this bot's reconcile hold. A failed write stays in
        `control.hold_unwritten`: held in memory, retried on every check."""
        try:
            await self.control.set_reconcile_hold(True)
        except Exception:
            logger.exception("Restore guard: writing the reconcile hold failed")
            return False
        return True

    async def _secure_lost_control_state(self) -> None:
        """Guard 1: Redis lost the control state (sentinel missing). Sets
        this bot's reconcile hold — never a pause (operator decision 5.4:
        exits must run) nor the mode-wide kill switch (other tenants) —
        queues one alert, and writes the sentinel only once the hold
        landed, so a failed write comes back on the next check."""
        landed = await self._write_hold()
        if not self._lost_state_alerted:
            self._lost_state_alerted = True
            self._lost_state_at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            # What the loss reset; clearing the hold does not undo it.
            self._lost_state_effects = (
                "If Redis was emptied, disabled strategies, leverage "
                "overrides and the daily-loss counter were reset."
            ) + (
                " On mainnet the tenant's strategy opt-in set lived in this "
                "Redis too, so no strategy runs, exits included, until it "
                "is re-enabled." if settings.is_mainnet else ""
            )
            broken = "" if landed else (
                " WRITE FAILED: the hold is enforced in memory and the "
                "write retried every tick."
            )
            self._lost_state_alert = (
                f"Redis control state was lost at {self._lost_state_at} "
                "(sentinel missing: Redis flushed or recreated, or this "
                "build's first boot). "
                "Reconcile hold ON for this bot: it opens nothing and "
                "market-closes no exchange position without a DB row. "
                "Exits keep running; the bot is NOT paused. "
                f"{self._lost_state_effects} Open DB positions: "
                f"{await self._open_positions_text()}.{broken} "
                f"{self._clear_hold_how()}."
            )
        if not landed:
            return
        try:
            await self.control.write_sentinel()
        except Exception:
            logger.exception("Restore guard: writing the sentinel failed")
            return
        self._lost_state_alerted = False
        logger.warning("Restore guard: reconcile hold saved, sentinel written")

    def _clear_hold_how(self) -> str:
        return (
            "Check the book against the exchange first. Once the hold is "
            "cleared, a reconcile pass runs before any open: it "
            "market-closes a lone exchange position without a DB row on a "
            "coin this bot trades, sets the hold again while several "
            "remain, and leaves ones on coins it does not trade alone. "
            'Clear it with POST /api/control/reconcile-hold {"active": '
            "false} (bot API key) or redis-cli DEL "
            f"{self.control.reconcile_hold_key}"
        )

    async def _open_positions_text(self) -> str:
        try:
            rows = await self.repo.get_open_positions()
        except Exception:
            logger.exception("Restore guard: reading open positions failed")
            return "unreadable"
        return "; ".join(
            f"{r.strategy_name} {r.side} {r.size} {r.symbol}" for r in rows
        ) or "none"

    async def _remind_hold(
        self, hold: str | None, result: ReconcileResult | None,
        announced: bool, error: str | None = None,
    ) -> None:
        """Re-announce a standing hold, held positions, or opens that
        waited OPENS_STUCK_SECONDS for a pass that counts (as when every
        pass raises: `error`, no `result`) at most every
        HOLD_REALERT_SECONDS, so none goes quiet after one alert. A
        restart announces a hold still set once more. Only a delivered
        notice that named the held positions counts as an announcement."""
        now = time.monotonic()
        held = ", ".join(result.held_orphans) if result else ""
        waited = now - self._awaiting_since if self._awaiting else 0.0
        holding = hold == _HOLD_SET or bool(hold and held)
        if not holding and waited < OPENS_STUCK_SECONDS:
            # Nothing held, or only coins nobody here trades (nothing can
            # net into those): their notice, deduplicated until a size or
            # side changes, is the one announcement.
            self._hold_alert_at = None
            return
        if announced and holding:
            self._hold_alert_at = now
            return
        last = self._hold_alert_at
        if last is not None and now - last < HOLD_REALERT_SECONDS:
            return
        if hold == _HOLD_SET:
            message = (
                "Reconcile hold is still set: this bot opens nothing and "
                "market-closes no exchange position without a DB row "
                f"(held now: {held or 'none seen this pass'}). "
                f"{self._clear_hold_how()}."
            )
        elif holding:
            message = (
                "Reconcile is still holding exchange positions without a "
                f"DB row instead of closing them: {held} ({hold}). Decide "
                "on them by hand; reconcile will not close them while this "
                "holds."
            )
        else:
            message = (
                "Opens are blocked until a reconcile pass completes "
                f"({self._awaiting}, {int(waited // 60)} min so far). "
                "Exits keep running."
            )
        if error:
            message += f" The last pass raised {error}."
        if await self._publish_error("reconcile", message):
            self._hold_alert_at = now

    async def _on_reconcile_close(
        self, strategy_name: str, pnl: float | None = None, *,
        reset: bool = True,
    ) -> None:
        """Reconcile closed a DB row outside the normal signal path.

        Resets the strategy's in-memory state AND books the realised PnL
        into the portfolio. Before this, reconcile closes were invisible
        to `portfolio.record_pnl`, so the `MAX_DAILY_LOSS_USD` kill
        switch never saw the losses it exists to stop. `reset=False`
        books the PnL only (a flip whose old side is gone goes on).
        """
        if reset:
            await self._reset_strategy_state(
                strategy_name, why="reconcile closed its DB position",
            )
        if pnl is None:
            return
        try:
            await self.portfolio.record_pnl(float(pnl))
        except Exception:
            logger.exception(
                "Reconcile: failed to record PnL %.4f for %s", pnl, strategy_name,
            )

    async def _reset_strategy_state(
        self, strategy_name: str, why: str, *, quiet: bool = False,
    ) -> None:
        """Reset a strategy to flat, in memory AND in its Redis snapshot.

        Called whenever the DB says the strategy holds nothing while the
        strategy may believe otherwise: reconcile closed its row, flat-all,
        a flip that ended flat, a CLOSE ignored for want of an open row.
        Without the in-memory reset the strategy keeps _in_position=True
        while the DB shows closed → no re-entry possible. Without the
        snapshot write, the Redis snapshot keeps the state of its last
        executed signal — an OPEN — and the next restart restores it as in
        position (2026-09-23; startup now discards position flags anyway,
        this keeps the snapshot honest in between).

        `reset_position()`, not `reset_state()`: the position goes, the
        strategy's cooldown fields stay. hash_momentum's `reset_state()`
        also wipes its post-close cooldown — the cooldown its own CLOSE
        had just started when that CLOSE was then ignored.

        Every caller has found the strategy's row closed (or never open),
        so a pending close of it has nothing left to close and ends too.
        """
        for key in [k for k in self._pending_closes if k[0] == strategy_name]:
            del self._pending_closes[key]
        for s in self.strategies:
            if s.name == strategy_name:
                try:
                    s.reset_position()
                except Exception:
                    logger.exception("reset_position failed for %s", strategy_name)
                    return
                logger.log(
                    logging.DEBUG if quiet else logging.INFO,
                    "Reset %s to flat in memory and in its Redis snapshot "
                    "(%s)", strategy_name, why,
                )
                await self._save_strategy_snapshot(s)
                return

    async def _save_strategy_snapshot(self, strat: Strategy) -> None:
        """Write the strategy's `export_state()` to its Redis snapshot; a
        None export deletes the snapshot. Never raises."""
        if not self.control:
            return
        try:
            await self.control.save_strategy_state(
                strat.name, strat.export_state(),
            )
        except Exception:
            logger.exception(
                "[%s] save_strategy_state failed (non-fatal)", strat.name,
            )

    async def tick(self) -> None:
        """Run one cycle: fetch data, evaluate strategies, execute signals."""
        # Heartbeat first — even if downstream work fails, we want the
        # watchdog to know the runner is alive.
        if self.control:
            try:
                await self.control.beat_heartbeat()
            except Exception:
                logger.warning("Heartbeat write failed (continuing)")

        # NU-2 guard 1 on every tick, not only on the 5-minute reconcile:
        # a Redis emptied under a running bot reads as "not paused, nothing
        # disabled", and strategies run below.
        await self._check_control_state()

        # Periodic reconcile every 5 minutes. Catches positions closed
        # manually on the exchange, partial fills, and any post-startup
        # divergence between DB and exchange.
        if self.repo and await self._reconcile_due():
            try:
                await self._run_reconcile("Periodic")
            except Exception as exc:
                logger.exception("Periodic reconcile failed")
                # Exits still run below; opens that wait for this pass
                # are announced once they have waited too long.
                await self._remind_hold(
                    self._control_hold, None, False, type(exc).__name__,
                )
            self._last_reconcile = time.time()

        # Funding poll every 30 minutes. Backfills user_funding_history
        # since the most-recent stored payment (or 24h back on first run).
        if self.repo and (time.time() - self._last_funding_poll) > 1800:
            try:
                await self._poll_funding()
            except Exception:
                logger.exception("Funding poll failed")
            self._last_funding_poll = time.time()

        # HODL + vault scanner, on the services-owner bot only.
        await self._run_side_services()

        # Trade-rate anomaly alarm. Catches strategies that start
        # spam-trading vs their normal baseline (e.g. the 2026-05-09
        # hash_momentum stale-bar SL bug that fired 30 SOL trades in 4h).
        # Auto-pauses the offender via Redis disable_strategy + emits
        # an error event so Telegram routes the alert.
        if (
            settings.trade_rate_alarm_enabled
            and self.repo
            and self.control
            and (time.time() - self._last_rate_check)
                > settings.trade_rate_alarm_check_interval_seconds
        ):
            try:
                await self._check_trade_rate_anomalies()
            except Exception:
                logger.exception("Trade-rate alarm check failed")
            self._last_rate_check = time.time()

        # Honor flat-all request before everything else. The request is
        # acknowledged ONLY on full success: acknowledging a flat-all
        # that never read the exchange (or left closes failing) tells
        # the operator the book is flat when it is not. While a request
        # is pending, opens are held whatever this attempt's outcome, so
        # a standing entry does not re-open a coin flat-all just closed
        # while another coin keeps it from completing; exits still run.
        holds: list[str] = []
        paused_by_flat_all = False
        if self.control:
            try:
                pending = await self.control.get_pending_flat_request()
            except Exception:
                logger.warning(
                    "Flat-all request unreadable — no flat-all this tick; "
                    "opens held, exits run", exc_info=True,
                )
                pending = None
                holds.append("the flat-all request could not be read")
            if pending:
                holds.append("a flat-all request is pending")
                paused_by_flat_all = await self._honour_flat_all(pending)

        controls = await self._read_tick_controls()
        holds.extend(controls.unreadable)
        paused = controls.paused

        # A flat-all that just completed paused the bot; hold this tick
        # even if that pause write failed, so no strategy re-opens seconds
        # after the book was flattened.
        if paused_by_flat_all:
            paused = True

        # A startup state restore that never succeeded paused the bot
        # (`_halt_for_unrestored_state`). The first un-paused tick retries
        # it before any strategy runs; a failure pauses the bot again.
        if (
            not paused
            and self._restore_pending
            and not await self._retry_pending_restore()
        ):
            paused = True

        # Opens wait on top of the NU-2 hold, for this tick; exits run.
        tick_hold = "; ".join(holds) or None
        self._tick_hold = None
        if tick_hold and not self.portfolio.opens_held:
            self.portfolio.opens_held = self._tick_hold = tick_hold

        if paused:
            logger.info("Tick skipped — bot is paused")
        else:
            active = await self._strategies_for_tick(controls)
            logger.info(
                "Tick started — evaluating %d/%d strategies (disabled: %s)%s",
                len(active),
                len(self.strategies),
                sorted(controls.disabled) if controls.disabled else (
                    "unreadable" if controls.disabled is None else "none"
                ),
                f"; opens held: {tick_hold}" if tick_hold else "",
            )
            for strategy in active:
                await self._run_one(strategy)
            # NU-5a: a close the engine owes goes out whether or not its
            # strategy runs this tick (disabled, off the allowlist, or
            # auto-paused by the rate alarm); only a readable pause stops it.
            ran = {s.name for s in active}
            for strategy in self.strategies:
                if (
                    strategy.name not in ran
                    and (strategy.name, strategy.symbol) in self._pending_closes
                ):
                    await self._run_one(strategy, evaluate=False)
            # Aggregate this tick's fetch failures into the outage
            # window; alert once if the window persists. Runs after the
            # strategy loop (and only on non-paused ticks) so a paused
            # bot freezes — not closes — an open window.
            await self._settle_fetch_outage()

        # Update unrealized P&L for open positions (always, even when paused)
        if self.repo:
            try:
                await self._update_position_pnl()
            except Exception:
                logger.exception("Failed to update position P&L")

        # Snapshot equity (always, even when paused). A failed read is
        # skipped, not zeroed: `get_balance()` used to answer a 502 with
        # Balance(total=0), which wrote a $0 equity snapshot and made the
        # dashboard and the drawdown maths see a blown account.
        try:
            balance = await self.exchange.get_balance()
        except ExchangeReadError as e:
            logger.warning(
                "Equity snapshot skipped this tick — exchange read failed: %s", e,
            )
        except Exception:
            logger.exception("Failed to read balance for equity snapshot")
        else:
            try:
                if self.repo:
                    await self.repo.snapshot_equity(
                        balance.total, balance.available, balance.unrealized_pnl
                    )
            except Exception:
                logger.exception("Failed to snapshot equity")

            if self.event_bus:
                try:
                    positions = await self.exchange.get_positions()
                except ExchangeReadError as e:
                    logger.warning(
                        "Heartbeat position count unavailable — "
                        "exchange read failed: %s", e,
                    )
                except Exception:
                    logger.exception("Failed to read positions for heartbeat")
                else:
                    try:
                        await self.event_bus.publish(
                            BotHeartbeat(
                                mode=settings.exchange_mode,
                                strategies=",".join(
                                    s.name for s in self.strategies
                                ),
                                equity=balance.total,
                                positions=len(positions),
                                uptime_seconds=int(time.time() - _start_time),
                            )
                        )
                    except Exception:
                        logger.exception("Failed to publish heartbeat")

    async def _run_one(self, strategy: Strategy, *, evaluate: bool = True) -> None:
        """One strategy's turn in the tick, its errors classified.
        `evaluate=False` sends only the close the engine owes it."""
        try:
            if evaluate:
                await self._run_strategy(strategy)
            else:
                await self._run_strategy(strategy, evaluate=False)
        except TradeDbDivergence:
            # Already handled at source: bot paused, detailed
            # ErrorOccurred event published, full traceback
            # logged via logger.exception. Skip the generic
            # "Strategy tick failed" branch so we don't
            # double-publish + double-log the same incident
            # (Copilot review fix on PR #107).
            return
        except Exception as e:
            logger.exception("Error running strategy %s", strategy.name)
            # Filter by error type before publishing (CLAUDE.md
            # backlog Open-Low: "Suppress Telegram noise on
            # transient HL-fetch failures"; § 6 "Telegram is for
            # humans"). A transient HL/network error recovers on
            # the next tick — publishing per strategy per tick
            # spammed ~22 events/min during the 2026-05-09
            # outage. Route it into the fetch-outage aggregator
            # instead: a window persisting ≥
            # FETCH_OUTAGE_ALERT_SECONDS still alerts exactly
            # once (see _settle_fetch_outage). Non-transient
            # errors are real bugs → publish immediately,
            # unchanged.
            if _is_transient_network_error(e):
                self._tick_fetch_failures.add(strategy.name)
            elif self.event_bus:
                await self.event_bus.publish(
                    ErrorOccurred(
                        strategy=strategy.name,
                        message="Strategy tick failed",
                    )
                )

    async def _honour_flat_all(self, token: str) -> bool:
        """Run a pending flat-all request. True when it completed, which
        paused the bot. Otherwise the request stays pending and is retried
        every tick; "did not complete" is published once per request and
        reason, not once per tick (a coin whose close keeps failing would
        otherwise send one Telegram a minute)."""
        status = await self._flat_all_positions()
        if status.ok:
            await self._pause_after_flat_all(status)
            await self.control.acknowledge_flat_request(token)
            self._flat_all_notice = None
            return True
        notice = (token, status.reason)
        if notice == self._flat_all_notice:
            logger.info(
                "Flat-all still not complete (%s) — retried next tick; opens "
                "held, exits run", status.reason,
            )
            return False
        logger.error(
            "Flat-all NOT acknowledged (%s) — the request stays pending and "
            "retries every tick; opens are held until it completes, exits "
            "run; manual intervention may be required", status.reason,
        )
        if await self._publish_error("flat-all", (
            f"Flat-all did not complete: {status.reason}. Positions may "
            "still be open; the request stays pending and retries every "
            "tick. Until it completes this bot opens nothing; exits still run."
        )):
            self._flat_all_notice = notice
        return False

    async def _read_tick_controls(self) -> "_TickControls":
        """This tick's control flags. One that cannot be read holds opens
        for the tick and never stops an exit (NU-2, roadmap NU-5b): an
        unreadable pause is not a pause (a readable one freezes
        everything, decision 5.4), and an unreadable disabled set or
        mainnet allowlist runs only the strategies holding a position."""
        controls = _TickControls()
        if not self.control:
            return controls
        failures: list[str] = []

        async def read(what: str, call, *args):
            try:
                return True, await call(*args)
            except Exception as e:
                failures.append(f"{what} ({type(e).__name__}: {e})")
                controls.unreadable.append(f"the {what} could not be read")
                return False, None

        ok, value = await read("paused flag", self.control.is_paused)
        controls.paused = bool(value) if ok else False
        ok, value = await read(
            "disabled-strategy set", self.control.get_disabled_strategies,
        )
        controls.disabled = set(value or ()) if ok else None
        ok, value = await read(
            "leverage overrides", self.control.get_all_leverage_overrides,
        )
        # Overrides size opens; held this tick when unreadable.
        for s in self.strategies if ok else ():
            if s.name in (value or {}):
                s.leverage = value[s.name]
        if settings.is_mainnet and settings.tenant_id:
            # Per-tenant mainnet allowlist (layer 2). Empty set = nothing
            # trades on mainnet for this tenant. Re-read every tick so UI
            # toggles take effect without a bot restart.
            ok, value = await read(
                "mainnet allowlist",
                self.control.get_mainnet_enabled_strategies_for_tenant,
                settings.tenant_id,
            )
            controls.mainnet_enabled = set(value or ()) if ok else None
            controls.allowlist_unreadable = not ok
        if failures:
            logger.warning(
                "Control flags unreadable: %s — this tick opens nothing, "
                "exits run%s", "; ".join(failures),
                " (only for strategies holding a position)"
                if controls.disabled is None or controls.allowlist_unreadable
                else "",
            )
        return controls

    async def _strategies_for_tick(self, controls: "_TickControls") -> list:
        """The strategies that run this tick. With the disabled set or the
        mainnet allowlist unreadable, those holding a position (a DB row,
        or a close the engine owes) still run, for their exits; the tick
        hold refuses their opens."""
        if controls.disabled is not None and not controls.allowlist_unreadable:
            return filter_strategies_for_tick(
                self.strategies, controls.disabled, controls.mainnet_enabled,
            )
        holders = {k[0] for k in self._pending_closes}
        rows = None
        if self.repo is not None:
            try:
                rows = await self.repo.get_open_positions()
            except Exception:
                logger.warning(
                    "Open positions unreadable too — running the strategies "
                    "that believe they hold one", exc_info=True,
                )
        if rows is None:
            holders |= {s.name for s in self.strategies if _holds(s)}
        else:
            holders |= {p.strategy_name for p in rows}
        return [
            s for s in filter_strategies_for_tick(
                self.strategies, controls.disabled or set(),
                None if controls.allowlist_unreadable else controls.mainnet_enabled,
            )
            if s.name in holders
        ]

    async def _settle_fetch_outage(self, now: float | None = None) -> None:
        """Aggregate this tick's transient fetch failures into outage
        windows and decide whether Telegram should hear about it.

        Rate-limit/dedup policy (CLAUDE.md § 6 "Telegram is for humans"):
          - a window that clears before
            `settings.fetch_outage_alert_seconds` produces NO
            notification — the bot auto-recovers on the next tick and
            the per-strategy warning/error logs are the diagnostic trail;
          - a window persisting ≥ the threshold produces EXACTLY ONE
            ErrorOccurred per window, summarizing every strategy
            affected so far, so a genuine outage still reaches Telegram
            while it is ongoing (not just after it ends);
          - recovery (a tick with zero fetch failures) closes the
            window with a log summary only — never a second
            notification.

        Called from tick() after the strategy loop; only non-paused
        ticks settle, so a paused bot freezes (doesn't close) an open
        window. `now` is injectable for deterministic tests.
        """
        if now is None:
            now = time.time()
        failures = self._tick_fetch_failures
        self._tick_fetch_failures = set()

        if not failures:
            if self._outage_start is None:
                return  # quiet tick, no window open
            # Recovery: first tick with zero fetch failures closes the
            # window. Log-only — the user was pinged at most once for
            # this window (and not at all if it was a short blip), so
            # pinging the recovery would double the noise for zero info.
            logger.info(
                "Candle-fetch outage window closed after %.0fs — "
                "affected strategies (%d): %s%s",
                now - self._outage_start,
                len(self._outage_affected),
                ", ".join(sorted(self._outage_affected)) or "none",
                " — outage alert was sent"
                if self._outage_alerted
                else " — no alert (recovered before threshold)",
            )
            self._outage_start = None
            self._outage_affected = set()
            self._outage_alerted = False
            return

        if self._outage_start is None:
            self._outage_start = now
            self._outage_affected = set()
            self._outage_alerted = False
            logger.info(
                "Candle-fetch outage window opened — failing this tick: %s",
                ", ".join(sorted(failures)),
            )
        self._outage_affected.update(failures)

        threshold = settings.fetch_outage_alert_seconds
        if (
            threshold > 0
            and not self._outage_alerted
            and (now - self._outage_start) >= threshold
        ):
            # Latch: exactly one notification per outage window, no
            # matter how long it lasts or how many strategies join it.
            self._outage_alerted = True
            names = ", ".join(sorted(self._outage_affected))
            summary = (
                f"Candle fetch / network failures persisting for "
                f"{int(now - self._outage_start)}s across "
                f"{len(self._outage_affected)} strategy(ies): {names}. "
                f"The bot retries every tick and recovers on its own "
                f"once the exchange is back; investigate if it lasts "
                f"much longer."
            )
            logger.warning(
                "[%s] fetch-outage alert (one per window): %s",
                settings.exchange_mode,
                summary,
            )
            if self.event_bus:
                try:
                    await self.event_bus.publish(
                        ErrorOccurred(strategy="candle-fetch", message=summary)
                    )
                except Exception:
                    logger.exception("fetch-outage: event publish failed")

    async def _pause_after_flat_all(self, status: "FlatAllStatus") -> None:
        """A completed flat-all pauses the bot.

        Flat-all runs before the strategy loop of the same tick, and it
        resets every strategy whose row it closed. A strategy whose entry
        is a standing condition (sma_rsi, penguin_volatility) would send
        its OPEN again seconds after the operator flattened the book.
        Resuming is a deliberate operator action (/resume, the dashboard).
        The caller also holds the current tick, so a failed pause write
        still stops this tick's opens; it is reported loudly.
        """
        message = (
            f"Flat-all complete ({status.closed} position(s) closed). The "
            f"bot is now PAUSED so no strategy re-opens; resume it "
            f"deliberately (/resume or the dashboard) when you want it "
            f"trading again."
        )
        try:
            await self.control.set_paused(True)
        except Exception:
            logger.exception("Flat-all: pausing the bot failed")
            message = (
                f"Flat-all complete ({status.closed} position(s) closed), "
                f"but PAUSING THE BOT FAILED — strategies will trade again "
                f"from the next tick. Pause it manually."
            )
        logger.warning("%s", message)
        await self._publish_error("flat-all", message)

    async def _flat_all_positions(self) -> "FlatAllStatus":
        """Close every open position with a reduce-only market order.

        Returns a status instead of None so the caller can refuse to
        acknowledge the request. `ok` is true only when the exchange was
        actually readable, every position it showed is gone, and no open
        DB row is left — "I could not read the exchange" used to look
        identical to "there was nothing to close".

        The rows are checked after the closes (`_flat_all_settle_rows`):
        a row holding more than the exchange did (a partial liquidation,
        a close by hand, a stale read) keeps what the fill did not cover,
        and once a confirmed read shows none of it left, that rest is
        booked as an external close priced from fills and its strategy is
        reset. Acknowledging with it still open left DB and exchange apart
        for the whole pause that follows, with reconcile in dry run.
        """
        try:
            positions = await self.exchange.get_positions()
        except Exception as e:
            # One handler: whatever the type, we did not get to look at
            # the book, so the answer is the same. Flat-all is a rare
            # operator action, so the traceback is worth having.
            logger.exception(
                "Flat-all: exchange read failed — NOT treating the book as "
                "flat and NOT acknowledging the request",
            )
            return FlatAllStatus(
                ok=False,
                reason=f"exchange read failed ({type(e).__name__}): {e}",
            )

        if positions:
            logger.warning("Flat-all closing %d positions", len(positions))
        # coin -> why it is not flat. Worded without live numbers, so the
        # same failure on the next tick reads the same (one notice).
        failed: dict[str, str] = {}
        # coin -> (side, price) of a close that filled: what its rows keep
        # beyond the fill, no fill covering it, is size drift at that price.
        filled_at: dict[str, tuple[str, float]] = {}
        closed = 0
        for pos in positions:
            try:
                why = await self._flat_all_close(pos, filled_at)
            except Exception as e:
                logger.exception(
                    "Flat-all: closing %s %s %s failed",
                    pos.side, pos.size, pos.symbol,
                )
                why = f"the close raised {type(e).__name__}"
            if why:
                failed[pos.symbol] = why
            else:
                closed += 1
        await self._flat_all_settle_rows(failed, filled_at)

        if failed:
            # The caller logs it, once per request and reason.
            reason = "; ".join(f"{sym}: {why}" for sym, why in sorted(failed.items()))
            return FlatAllStatus(
                ok=False, closed=closed, failed=len(failed), reason=reason,
            )
        if not positions:
            logger.info("Flat-all requested — no open positions")
        return FlatAllStatus(ok=True, closed=closed)

    async def _flat_all_close(
        self, pos, filled_at: dict[str, tuple[str, float]],
    ) -> str | None:
        """Close one exchange position for flat-all and book what filled,
        noting its side and price in `filled_at`. None when none of it is
        left; else why it is still open."""
        close_side = "sell" if pos.side == "long" else "buy"
        read = float(pos.size)  # what the read showed, whatever `pos` becomes
        # Audit H5+H6 + PR #31 review: get EVERY open DB row for this
        # coin (not just one — `allow_multi_coin=True` lets multiple
        # strategies hold rows on the same coin). The exchange shows one
        # netted position; the close fee/PnL is split across the rows on
        # its side by size weight, each with its own entry_price. A row on
        # the other side was not closed by this order and is left to
        # `_flat_all_settle_rows`.
        db_recs = [
            r for r in (
                await self.repo.get_open_positions_for_symbol(pos.symbol)
                if self.repo else []
            )
            if r.side == pos.side
        ]
        order = await self.exchange.place_order(
            pos.symbol, close_side, read, OrderType.MARKET,
            reduce_only=True,
        )
        filled = float(order.size) if order.status.value == "filled" else 0.0
        if filled > 0:
            if order.filled_price:
                filled_at[pos.symbol] = (pos.side, float(order.filled_price))
            await self._flat_all_book(pos, read, order, filled, db_recs)
        # Only what filled is booked (reduce-only fills at most what is
        # left); less than was read is checked against a confirmed read.
        left = self._unfilled(pos.symbol, read, filled) if filled > 0 else read
        if not left:
            return None
        why = (
            "filled short" if filled > 0
            else f"{order.status.value}"
            f" ({getattr(order, 'error', None) or 'no reason given'})"
        )
        held, ex_pos, exchange = await self._held_after_close(pos.symbol, pos.side)
        if not held:
            logger.warning(
                "Flat-all: %s %s %s %s (filled %s); the exchange holds none "
                "of it now (%s), so the read was stale — nothing left to close",
                pos.side, read, pos.symbol, why, filled, exchange,
            )
            return None
        logger.warning(
            "Flat-all: %s %s %s %s — filled %s of %s, %s still open "
            "(exchange: %s)", pos.side, read, pos.symbol, why, filled,
            read, _held_on_side(ex_pos, pos.side) or left, exchange,
        )
        return f"{why}, still open"

    async def _flat_all_book(
        self, pos, read: float, order, filled: float, db_recs: list,
    ) -> None:
        """Book a flat-all fill of `filled` (of `read`) on the rows it closed.

        One exchange order closes every row on the coin's side, and
        `trades.order_id` is UNIQUE: the first row gets the oid, each next
        one `<oid>-<n>`. One shared id failed every row after the first,
        which stayed open against a flat exchange (booking-2)."""
        filled_price = order.filled_price or 0
        reason = "Flat-all from dashboard"
        if getattr(order, "late", None) == "mid":
            reason = f"ESTIMATED {reason} (filled after a timeout, @ mid)"
        if not db_recs:
            # No DB-side open record (rare exchange-side orphan).
            # Best-effort: record one trade for history with the size
            # that filled, using the exchange's VWAP entry as the only
            # entry signal we have.
            fee = filled_price * filled * settings.taker_fee_rate
            pnl = realized_pnl(
                side=pos.side, entry_price=pos.entry_price,
                exit_price=filled_price, size=filled, fee=fee,
            )
            if self.repo:
                await self.repo.record_trade(
                    order_id=order.id,
                    strategy_name="manual_flat",
                    symbol=pos.symbol,
                    side=order.side,
                    size=filled,
                    price=filled_price,
                    fee=fee,
                    pnl=pnl,
                    reason=f"{reason} (no open DB rec)",
                )
            await self.portfolio.record_pnl(pnl)
            logger.warning(
                "Flat-all: closed %s %s of %s %s @ %.2f with no DB row "
                "(PnL %.2f)", pos.side, filled, read, pos.symbol,
                filled_price, pnl,
            )
            return

        # Split close fee + PnL across each open DB row by its share of
        # total open size. Per-row PnL uses that row's own entry_price
        # (audit H6) — exchange VWAP would be wrong when two strategies
        # opened at different prices. Less filled than the rows hold:
        # each books its share and keeps the rest open for now.
        total_db_size = sum(float(r.size) for r in db_recs) or 1.0
        short = self._unfilled(pos.symbol, total_db_size, filled)
        scale = filled / total_db_size if short else 1.0
        for n, rec in enumerate(db_recs, start=1):
            share = float(rec.size) / total_db_size
            rec_size = float(rec.size) * scale
            rec_fee = filled_price * rec_size * settings.taker_fee_rate
            rec_entry = float(rec.entry_price)
            rec_pnl = realized_pnl(
                side=rec.side, entry_price=rec_entry,
                exit_price=filled_price, size=rec_size, fee=rec_fee,
            )
            # Audit H5: single atomic trade+close per row.
            await self.repo.record_trade_and_close_position(
                order_id=order.id if n == 1 else f"{order.id}-{n}",
                strategy_name=rec.strategy_name,
                symbol=pos.symbol,
                trade_side=order.side,
                size=rec_size,
                price=filled_price,
                fee=rec_fee,
                pnl=rec_pnl,
                reason=reason,
                remaining=float(rec.size) - rec_size if short else 0.0,
            )
            await self.portfolio.record_pnl(rec_pnl)
            logger.warning(
                "Closed [%s] %s %s @ %.2f (entry %.2f, size %.6f, share %.0f%%, PnL %.2f)",
                rec.strategy_name, rec.side, pos.symbol,
                filled_price, rec_entry, rec_size, share * 100, rec_pnl,
            )
            # The strategy did not ask for this close; without the reset
            # it keeps believing in the position and never re-enters. One
            # whose row keeps a rest keeps it.
            if not short:
                await self._reset_strategy_state(
                    rec.strategy_name, why="flat-all closed its DB position",
                )
        if short:
            logger.warning(
                "Flat-all: %s filled %s of the rows' %s — each keeps its "
                "share of the rest until the exchange is read again",
                pos.symbol, filled, total_db_size,
            )

    async def _flat_all_settle_rows(
        self, failed: dict[str, str], filled_at: dict[str, tuple[str, float]],
    ) -> None:
        """After flat-all's closes, every open DB row must be gone too. A
        row the exchange (confirmed) no longer holds is booked as an
        external close priced from fills — or, the rest of a row whose
        coin's close filled on its side, as size drift at that fill — and
        its strategy reset; one it still holds, or one that cannot be
        booked, fails the attempt."""
        if not self.repo:
            return
        try:
            rows = await self.repo.get_open_positions()
        except Exception as e:
            logger.exception("Flat-all: reading the open rows after the closes failed")
            failed["DB"] = f"open rows unreadable ({type(e).__name__})"
            return
        for row in rows:
            if row.symbol in failed:
                continue  # its coin's close failed: that is the reason
            held, ex_pos, exchange = await self._held_after_close(
                row.symbol, row.side,
            )
            if held:
                logger.warning(
                    "Flat-all: [%s] %s %s %s is still held on the exchange "
                    "(%s), which the flat-all read did not show",
                    row.strategy_name, row.side, row.size, row.symbol, exchange,
                )
                failed[row.symbol] = "still held on the exchange"
            elif not await self._book_external_close(
                row, f"flat-all found none of it on the exchange ({exchange})",
                ex_pos, drift_price=(
                    filled_at[row.symbol][1]
                    if filled_at.get(row.symbol, ("",))[0] == row.side else None
                ),
            ):
                failed[row.symbol] = "a row the exchange no longer holds could not be booked"

    async def _update_position_pnl(self) -> None:
        """Update unrealized P&L for all open positions in the DB."""
        if not self.repo:
            return
        open_positions = await self.repo.get_open_positions()
        for pos in open_positions:
            current_price = await self.exchange.get_current_price(pos.symbol)
            if current_price <= 0:
                continue
            # Mark-to-market: the same side-aware formula with the
            # current price as the exit and no fee.
            pnl = realized_pnl(
                side=pos.side, entry_price=pos.entry_price,
                exit_price=current_price, size=pos.size,
            )
            await self.repo.update_position_pnl(pos.id, pnl)

    async def _run_strategy(self, strategy: Strategy, *, evaluate: bool = True) -> None:
        """One strategy's tick: fetch candles, ask it for a signal, execute
        it. `evaluate=False` (a strategy that does not run this tick) only
        sends the close the engine owes it (NU-5a)."""
        key = (strategy.name, strategy.symbol)
        # Fetch candles
        candles = await fetch_candles(strategy.symbol, strategy.timeframe)
        if candles.empty:
            logger.warning("No candle data for %s %s", strategy.symbol, strategy.timeframe)
            # fetch_candles swallows network/5xx errors after its 3
            # tenacity retries and returns an empty frame — this IS the
            # fetch-failure signature during an HL outage (only bare
            # TimeoutError propagates). Record it for the outage-window
            # aggregator so a persistent outage still alerts once (see
            # _settle_fetch_outage) instead of staying fully silent.
            self._tick_fetch_failures.add(strategy.name)
            # A close the engine owes needs no candles: it still goes.
            if key in self._pending_closes:
                self._note_pre_signal_state(strategy)
                await self._send_pending_close(strategy, None, 0.0)
            return

        # Update exchange price (use latest/forming candle for real-time pricing)
        latest_price = candles["close"].iloc[-1]
        # DEBUG not INFO: one line per strategy per tick (22 strategies x
        # every 60s) was ~1,000 lines/hour per bot and the single biggest
        # log-volume source (bot/reports/analysis-2026-09-15.md § 1). The
        # "Tick started" summary line above and every signal/open/close/
        # skip/reconcile line stay at INFO — those are the API (CLAUDE.md
        # § 6 "Logs are the API"); this one is per-tick telemetry.
        logger.debug("[%s] %s %s — %d candles, price: $%.2f", strategy.name, strategy.symbol, strategy.timeframe, len(candles), latest_price)
        if hasattr(self.exchange, "set_price"):
            self.exchange.set_price(strategy.symbol, latest_price)

        # Evaluate strategy only on CLOSED candles to avoid repeatedly
        # re-firing signals based on the forming candle's changing values.
        closed_candles = candles.iloc[:-1] if len(candles) > 1 else candles
        bar_time = (
            closed_candles["timestamp"].iloc[-1]
            if "timestamp" in closed_candles.columns else None
        )
        if not evaluate:
            self._note_pre_signal_state(strategy)
            await self._send_pending_close(strategy, None, latest_price, bar_time)
            return
        # NU-5a: the state a signal this tick may have to take back.
        self._note_pre_signal_state(strategy)
        try:
            signal = await strategy.on_candle(closed_candles)
        except Exception:
            # A strategy that raises still gets the close the engine owes.
            if key in self._pending_closes:
                await self._send_pending_close(
                    strategy, None, latest_price, bar_time,
                )
            raise
        signal_action = "none"
        signal_reason = ""
        pending = self._pending_closes.get(key)
        if pending is not None and not _closes_side(signal, pending.side):
            # The close the engine owes goes out whatever the rule says
            # now; a signal that closes the row itself is used instead.
            await self._send_pending_close(strategy, signal, latest_price, bar_time)
            signal = None

        if signal is not None and signal.action != SignalAction.HOLD:
            signal_action = signal.action.value
            signal_reason = signal.reason

            logger.info(
                "[%s] Signal: %s %s — %s",
                strategy.name,
                signal.action.value,
                signal.symbol,
                signal.reason,
            )

            await self._execute_signal(
                signal, latest_price, leverage=strategy.leverage,
                bar_time=bar_time,
            )

        # Publish tick result
        if self.event_bus:
            await self.event_bus.publish(
                TickCompleted(
                    strategy=strategy.name,
                    symbol=strategy.symbol,
                    timeframe=strategy.timeframe,
                    price=latest_price,
                    signal=signal_action,
                    reason=signal_reason,
                )
            )

    def _note_pre_signal_state(self, strategy: Strategy) -> None:
        """Keep what a signal this tick may have to take back (NU-5a): a
        key iff the strategy holds a position now, with its export."""
        self._pre_signal_state.pop(strategy.name, None)
        if not _holds(strategy):
            return
        try:
            state = strategy.export_state()
        except Exception:
            logger.debug("[%s] no pre-signal state kept", strategy.name, exc_info=True)
            state = None
        self._pre_signal_state[strategy.name] = (
            dict(state) if isinstance(state, dict) else None
        )

    async def _send_pending_close(
        self, strategy: Strategy, dropped: Signal | None, price: float,
        bar_time=None,
    ) -> None:
        """NU-5a: send the close the engine owes `strategy`'s row, through
        the standard close path (DB size, reduce-only, the band its misses
        earned, the same classification). Ends it without an order when
        the row is gone."""
        key = (strategy.name, strategy.symbol)
        pending = self._pending_closes[key]
        row = await self.repo.get_open_position(*key)
        if row is None:
            await self._reset_strategy_state(
                strategy.name,
                why=f"the {pending.side} {strategy.symbol} row its pending "
                    "close was for is closed",
            )
            return
        if row.side != pending.side:
            del self._pending_closes[key]
            logger.info(
                "[%s] its pending close of a %s %s ends: the row there is "
                "%s now", strategy.name, pending.side, strategy.symbol, row.side,
            )
            return
        if dropped is not None and dropped.action != SignalAction.HOLD:
            logger.info(
                "[%s] %s %s not executed — the close the engine owes its %s "
                "goes first", strategy.name, dropped.action.value,
                dropped.symbol, row.side,
            )
        # It drops the position, as a strategy does when it emits its exit:
        # a fill leaves it flat, a miss re-arms it with this state.
        if _holds(strategy):
            self._note_pre_signal_state(strategy)
        try:
            strategy.reset_position()
        except Exception:
            logger.exception("[%s] reset_position failed", strategy.name)
        signal = Signal(
            action=(
                SignalAction.CLOSE_LONG if row.side == "long"
                else SignalAction.CLOSE_SHORT
            ),
            symbol=strategy.symbol,
            strategy_name=strategy.name,
            reason=f"{pending.reason} (pending close, attempt {pending.misses + 1})",
        )
        logger.info(
            "[%s] Pending close: %s %s %s — %s", strategy.name,
            signal.action.value, row.size, signal.symbol, signal.reason,
        )
        await self._execute_signal(
            signal, price, leverage=strategy.leverage, bar_time=bar_time,
        )

    async def _execute_signal(
        self,
        signal: Signal,
        current_price: float,
        leverage: int = 1,
        bar_time=None,
    ) -> bool:
        """Execute a signal end-to-end. Returns True on full success
        (order filled + DB written + parity OK), False on any abort
        (risk-blocked, kill-switch, coin/family/opposite-side conflict,
        exposure cap, size ceiling, leverage push failed, order rejected,
        close-size unresolved). Callers performing flip-close-then-open
        MUST check the close return value — opening a new opposite
        position when the close failed leaves the DB and exchange
        permanently divergent.

        An OPEN is checked in full BEFORE anything is sent. When the
        strategy holds the opposite side (a flip), every gate that can
        refuse the new open — coin, family, opposite side, exposure, size
        ceiling, the leverage push, and the risk limits (kill switch,
        daily-loss cap, the NU-2 hold) — runs before the synthesized close.
        A refusal refuses only the open half: the close half still goes
        out and the strategy ends flat (`_close_half_of_refused_flip`),
        because closing only reduces exposure and a flip is the only exit
        hash_supertrend and kalman_breakout have — exits are never blocked.
        Whatever happens, the strategy ends up believing in the position
        the DB actually holds.

        `bar_time` is the closed bar the signal came from; it keys the
        refusal dedup (see `_note_refusal`).
        """
        is_open = signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT)
        wanted_side = "long" if signal.action == SignalAction.OPEN_LONG else "short"

        # Idempotency / flip detection: don't open a same-side duplicate;
        # if we already hold the opposite side, this open is a flip and a
        # synthesized CLOSE runs first (below, after every check), so the
        # exchange position fully reverses instead of HL netting the new
        # open against the existing one and leaving a partial.
        existing = None
        if is_open and self.repo:
            try:
                existing = await self.repo.get_open_position(
                    signal.strategy_name, signal.symbol
                )
            except Exception:
                await self._open_unchecked(signal, wanted_side)
                raise
            if existing and existing.side == wanted_side:
                logger.info(
                    "[%s] Skipping %s %s — already have open %s position",
                    signal.strategy_name,
                    signal.action.value,
                    signal.symbol,
                    existing.side,
                )
                return False

        # Audit H3: kill-switch + daily-loss cap apply ONLY to OPEN signals.
        # Blocking CLOSE on kill-switch flip would freeze a position-open
        # bot (SL/TP exits wouldn't fire); blocking CLOSE on daily-loss cap
        # would prevent the loss from being realized and capped. That
        # includes the close half of a flip: hash_supertrend and
        # kalman_breakout have no SL, so the flip is their only exit, and
        # refusing it would trap a losing position past MAX_DAILY_LOSS_USD
        # until UTC midnight.
        if not await self.portfolio.check_risk_limits(is_open=is_open):
            # This bot's reconcile hold (NU-2), or this tick's hold (a
            # flat-all request, an unreadable control flag), says so, not
            # "risk limit"; each is announced on its own, so a plain open
            # is only logged.
            held = is_open and self.portfolio.opens_held
            tick = bool(held) and held == self._tick_hold
            category = (
                "opens held" if tick
                else "reconcile hold" if held else "risk limit"
            )
            head = (
                f"Opens held ({held})" if tick
                else f"Reconcile hold ({held})" if held
                else "Risk limit breached"
            )
            if existing is None:
                if is_open:
                    await self._flat_after_refused_open(
                        signal, category, bar_time,
                        f"{head} — execution of "
                        f"{signal.action.value} {signal.symbol} blocked",
                        publish=not held,
                    )
                else:
                    await self._note_refusal(
                        signal, category, bar_time,
                        f"{head} — execution of "
                        f"{signal.action.value} {signal.symbol} blocked",
                        publish=True,
                    )
                return False
            await self._close_half_of_refused_flip(
                signal, existing, current_price, leverage, bar_time,
                category, head,
                tail=f". {self._clear_hold_how()}" if held == _HOLD_SET else "",
                publish=True,
            )
            return False

        # `is not None` rather than truthy: a strategy emitting size=0 is
        # a bug we should surface, not silently fall back to the default
        # calc. Downstream gates (rounded-to-zero in HL place_order) will
        # reject it cleanly.
        size = (
            signal.size if signal.size is not None
            else self._calculate_size(current_price, leverage)
        )

        flipped = False
        close_row = None
        if is_open:
            try:
                refusal = await self._open_refusal(
                    signal, wanted_side, size, current_price, leverage,
                )
            except Exception:
                if existing is None:
                    raise
                # A flip must not die half-way through its checks: the
                # strategy already switched to the side it asked for, while
                # the DB and exchange still hold the old one.
                logger.warning(
                    "[%s] open gates for the %s flip on %s could not be "
                    "read", signal.strategy_name, wanted_side, signal.symbol,
                    exc_info=True,
                )
                refusal = (
                    logging.WARNING, "gate read failed",
                    "gate read failed (see the traceback above)",
                )

            # Audit H1: re-push per-coin leverage to HL before any OPEN if a
            # runtime override has bumped the strategy's `s.leverage` since
            # we last pushed. Per-coin leverage on HL is a single value, so
            # the target is `max(s.leverage)` across strategies trading this
            # coin — matching the startup logic in main.py. No-op when
            # already in sync. CLOSE signals don't need this (leverage
            # affects margin, which only applies to opens). A failed push
            # refuses the open; a flip's close half still goes.
            if refusal is None and not await self._ensure_leverage_pushed(
                signal.symbol,
            ):
                refusal = (
                    logging.WARNING, "leverage push failed",
                    f"leverage push for {signal.symbol} failed",
                )

            if refusal is not None:
                level, category, reason = refusal
                if existing is not None:
                    await self._close_half_of_refused_flip(
                        signal, existing, current_price, leverage, bar_time,
                        category, f"Flip {existing.side}→{wanted_side} on "
                        f"{signal.symbol}: open refused — {reason}",
                        level=level,
                    )
                else:
                    await self._flat_after_refused_open(
                        signal, category, bar_time,
                        f"Skipping {signal.action.value} {signal.symbol} "
                        f"— {reason}",
                        level=level,
                    )
                return False

            if existing is not None:
                if not await self._flip_close(
                    signal, existing, current_price, leverage, bar_time,
                    why=f"before opening {wanted_side}",
                ):
                    return False
                flipped = True

        if is_open:
            side = "buy" if signal.action == SignalAction.OPEN_LONG else "sell"
            try:
                order = await self.exchange.place_order(
                    signal.symbol, side, size, OrderType.MARKET
                )
            except Exception as e:
                if flipped:
                    await self._flat_after_flip_open_failed(
                        signal, f"the open order raised {type(e).__name__}",
                        bar_time, publish=False,
                    )
                raise
        elif signal.action in (SignalAction.CLOSE_LONG, SignalAction.CLOSE_SHORT):
            held = "long" if signal.action == SignalAction.CLOSE_LONG else "short"
            try:
                close_size, close_row = await self._resolve_close_size(
                    signal.strategy_name, signal.symbol, held
                )
                if close_size is None:
                    return False
                # Reduce-only (NU-5a): whatever the size, a close can shrink
                # the position and nothing else — never open or flip one.
                # After K attempts that left the row open, the wider band.
                pending = self._pending_closes.get(
                    (signal.strategy_name, signal.symbol),
                )
                band = (
                    {"slippage": WIDE_CLOSE_BAND}
                    if pending is not None
                    and pending.misses >= CLOSE_REJECTS_BEFORE_WIDE_BAND
                    else {}
                )
                order = await self.exchange.place_order(
                    signal.symbol, "sell" if held == "long" else "buy",
                    close_size, OrderType.MARKET, reduce_only=True, **band,
                )
            except Exception as e:
                await self._close_raised(signal, held, close_row, e)
                raise
            size = close_size
        else:
            return False

        if self.event_bus:
            await self.event_bus.publish(
                SignalGenerated(
                    strategy=signal.strategy_name,
                    symbol=signal.symbol,
                    action=signal.action.value,
                    reason=signal.reason,
                )
            )

        if order.status.value != "filled" or (not is_open and order.size <= 0):
            if not is_open:
                # One line per attempt, with the count, band, reason and
                # exchange state: `_close_left_open` writes it.
                await self._close_left_open(signal, order, close_row)
                return False
            detail = f"order {order.status.value}" + (
                f": {order.error}" if getattr(order, "error", None) else ""
            )
            logger.warning(
                "[%s] %s %s %s not filled: %s", signal.strategy_name,
                signal.action.value, size, signal.symbol, detail,
            )
            if flipped:
                await self._flat_after_flip_open_failed(
                    signal, detail, bar_time, publish=True,
                )
            return False

        # This (strategy, coin) traded again; its next refusal is news.
        self._refusal_notes.pop((signal.strategy_name, signal.symbol), None)

        # From here on `size` is what the exchange FILLED, not what we
        # asked for: HyperLiquid rounds to the coin's szDecimals (and an
        # IOC can fill short), and `Order.size` carries the filled amount.
        # The fee, the realised PnL, the trade and position rows and the
        # TradeExecuted event all used the unrounded request, which is the
        # live "size mismatch" reconcile warning (analysis-2026-09-15.md
        # § 4). A close that filled short keeps its unfilled rest open on
        # its row (`_unfilled`); one that filled it all closes the row.
        if abs(order.size - size) > 1e-12:
            logger.info(
                "[%s] %s %s filled %s (requested %s)",
                signal.strategy_name, signal.action.value, signal.symbol,
                order.size, size,
            )
        # A close's remainder is against its row, not the clamped request:
        # what the clamp left out is gone, and is booked below (NU-5a).
        remaining = 0.0 if is_open else self._unfilled(
            signal.symbol, float(close_row.size) if close_row else size,
            order.size,
        )
        # A close that closed its row ends the close the engine owed it; a
        # short fill keeps it, and counts as a miss (`_close_left_open`).
        if not is_open and not remaining and (
            pending := self._pending_closes.pop(
                (signal.strategy_name, signal.symbol), None,
            )
        ):
            logger.info(
                "[%s] %s %s filled after %d attempt(s) that left it open",
                signal.strategy_name, signal.action.value, signal.symbol,
                pending.misses,
            )
        size = order.size

        filled_price = order.filled_price or 0
        fee = filled_price * size * settings.taker_fee_rate

        logger.info(
            "[%s] Executed: %s %s %.4f @ %.2f",
            signal.strategy_name,
            signal.action.value,
            signal.symbol,
            size,
            filled_price,
        )

        # Re-anchor the strategy's entry/SL/TP to the ACTUAL exchange fill
        # before we persist its state below. At signal time the strategy set
        # _entry_price from the closed bar's close; the real fill differs
        # (slippage / market fill / paper fill-at-current-price), so any
        # percentage/ATR bracket is slightly wrong. on_filled corrects it.
        # Fail-safe: the position is already open — a bad bracket re-derivation
        # must not crash the tick, so log + continue. Closes don't need this.
        if signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT):
            strat = next(
                (s for s in self.strategies if s.name == signal.strategy_name),
                None,
            )
            if strat is not None:
                side = "long" if signal.action == SignalAction.OPEN_LONG else "short"
                # Guard against a filled order with no usable price: re-anchoring
                # to 0 would zero out entry/SL/TP for every entry-derived
                # strategy. Better to keep the signal-time estimate than corrupt
                # it — the position is open either way.
                if filled_price > 0:
                    try:
                        strat.on_filled(side, filled_price)
                    except Exception:
                        logger.exception(
                            "[%s] on_filled failed (position open; continuing)",
                            signal.strategy_name,
                        )
                else:
                    logger.warning(
                        "[%s] OPEN filled with no price (filled_price=%s); "
                        "skipping on_filled re-anchor, keeping signal-time estimate",
                        signal.strategy_name,
                        order.filled_price,
                    )

        # Calculate realized P&L for closes
        close_pnl: float | None = None
        open_pos = None
        if signal.action in (SignalAction.CLOSE_LONG, SignalAction.CLOSE_SHORT):
            if self.repo:
                open_pos = await self.repo.get_open_position(
                    signal.strategy_name, signal.symbol
                )
                if open_pos:
                    close_pnl = realized_pnl(
                        side=open_pos.side, entry_price=open_pos.entry_price,
                        exit_price=filled_price, size=size, fee=fee,
                    )

        # Record to DB — atomic Trade + PositionRecord write. Audit M8:
        # pre-fix the trade and position writes were two separate sessions,
        # so a SIGTERM / crash between them left a Trade row with no
        # matching PositionRecord (or vice versa for close), which the
        # next reconcile loop would treat as a divergence and force-close.
        #
        # 2026-05-13 fix: this write must NEVER be allowed to silently
        # fail while the order has already been placed. The order side
        # (HL place_order) returns success → we log "Executed:" → if the
        # DB write then raises (e.g. asyncpg InvalidPasswordError after
        # a tenant role-password rotation by a sibling bot start), the
        # exception used to bubble up to `_run_strategy`'s catch-all
        # which logged a traceback and continued the next tick. The bot
        # would keep placing orders the DB never saw — exact divergence
        # mode that hit testnet 05:00–10:00 UTC. We now **pause the bot
        # via Redis** on any DB-write failure so subsequent ticks can't
        # add more divergent orders. Operator must restart the bot
        # (which provisions a fresh DATABASE_URL via the orchestrator)
        # then un-pause.
        if self.repo:
            try:
                if signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT):
                    import json as _json
                    side = "long" if signal.action == SignalAction.OPEN_LONG else "short"
                    strat = next(
                        (s for s in self.strategies if s.name == signal.strategy_name),
                        None,
                    )
                    state_json = None
                    if strat is not None:
                        try:
                            state = strat.export_state()
                            if state is not None:
                                state_json = _json.dumps(state)
                        except Exception:
                            logger.exception(
                                "[%s] export_state failed (continuing without)",
                                signal.strategy_name,
                            )
                    await self.repo.record_trade_and_open_position(
                        order_id=order.id,
                        strategy_name=signal.strategy_name,
                        symbol=signal.symbol,
                        trade_side=order.side,
                        position_side=side,
                        size=size,
                        price=filled_price,
                        fee=fee,
                        reason=signal.reason,
                        state_json=state_json,
                    )
                elif signal.action in (SignalAction.CLOSE_LONG, SignalAction.CLOSE_SHORT):
                    order_id, reason = order.id, signal.reason
                    if getattr(order, "late", None):
                        order_id, reason = await self._late_close_booking(
                            signal, order,
                        )
                    await self.repo.record_trade_and_close_position(
                        order_id=order_id,
                        strategy_name=signal.strategy_name,
                        symbol=signal.symbol,
                        trade_side=order.side,
                        size=size,
                        price=filled_price,
                        fee=fee,
                        pnl=close_pnl or 0,
                        reason=reason,
                        remaining=remaining,
                    )
                    await self.portfolio.record_pnl(close_pnl or 0)
            except Exception as db_exc:
                # The order is already on the exchange. We failed to
                # record it. STOP TRADING so we don't open more
                # untracked positions. Operator must intervene.
                logger.exception(
                    "[%s] CRITICAL: trade executed on exchange (order_id=%s) "
                    "but DB write FAILED — auto-pausing bot to prevent further "
                    "divergence. Resolve the underlying DB error, restart the "
                    "bot to pick up fresh credentials, then un-pause.",
                    signal.strategy_name,
                    order.id,
                )
                if self.control:
                    try:
                        await self.control.set_paused(True)
                    except Exception:
                        logger.exception("Failed to set paused flag")
                if self.event_bus:
                    try:
                        await self.event_bus.publish(
                            ErrorOccurred(
                                strategy=signal.strategy_name,
                                message=(
                                    f"DB-write divergence: order {order.id} "
                                    f"placed but trades-table insert failed "
                                    f"({type(db_exc).__name__}: {db_exc}). "
                                    f"Bot paused — restart to refresh DB credentials."
                                ),
                            )
                        )
                    except Exception:
                        logger.exception("Failed to publish divergence event")
                # Re-raise as a sentinel so `_run_strategy`'s catch-all
                # recognises this incident is already handled (paused +
                # published) and skips its generic "Strategy tick failed"
                # event/log. Copilot review fix on PR #107 — was a plain
                # `raise` which double-logged + double-published.
                raise TradeDbDivergence(
                    f"order {order.id} placed but DB write failed"
                ) from db_exc

        # What a close left on its row is booked if gone from the exchange
        # (from fills, else as size drift at this fill's price), else the
        # strategy is re-armed and the engine closes the rest.
        if remaining and await self._close_left_open(
            signal, None, open_pos, filled_at=filled_price or None,
        ) in ("external", "external-held"):
            remaining = 0.0

        # Snapshot post-signal strategy state to Redis. Covers the
        # cooldown-after-close gap (audit M6 / PR #19 review): position
        # table state_json only persists during in-position windows;
        # this Redis snapshot persists post-close cooldown so a restart
        # inside the cooldown window can't bypass the re-entry block. A
        # close whose export is None deletes the snapshot: skipping that
        # write left the OPEN-time snapshot behind to be restored as a
        # live position on the next boot.
        strat = next(
            (s for s in self.strategies if s.name == signal.strategy_name),
            None,
        )
        if strat is not None:
            await self._save_strategy_snapshot(strat)

        # Publish events
        if self.event_bus:
            await self.event_bus.publish(
                TradeExecuted(
                    strategy=signal.strategy_name,
                    symbol=signal.symbol,
                    side=order.side,
                    size=size,
                    price=filled_price,
                    order_id=order.id,
                    reason=signal.reason,
                )
            )

        # Parity check: after the trade and DB write, verify exchange
        # position for this coin matches the DB sum across all open
        # strategies. Catches partial-fill drift instantly (5min reconcile
        # is too slow — the 2026-05-09 SOL spam accumulated 1.1 SOL of
        # divergence in 4 hours before the 5min reconcile noticed).
        # Parity result propagates into the return value: a flip-detect
        # caller MUST NOT proceed to open if the close left exchange
        # diverged from DB (audit H1 / PR #19 review). Internal exceptions
        # are non-fatal but mismatch is.
        parity_ok = True
        try:
            parity_ok = await self._check_parity_after_trade(signal.symbol)
        except Exception:
            logger.exception(
                "parity check after %s %s failed (non-fatal)",
                signal.action.value, signal.symbol,
            )

        # A short-filled close did not finish: a flip must not open on it.
        return parity_ok and not remaining

    async def _late_close_booking(self, signal: Signal, order) -> tuple[str, str]:
        """The trade row's order id and reason for a close that filled only
        after its POST timed out (`Order.late`). A mid is an estimate, and
        the reason says so first (#167's convention): it reaches the trade
        listing, /eval and the daily-loss counter like a real fill. An oid
        that is already a trade row was another order's fill — a sibling's
        close on the coin, a flat-all — so its price is not this close's
        either, and its id would trip the UNIQUE `order_id` into the
        divergence pause: estimated too, under a fresh id. Never raises."""
        oid = order.id
        if order.late == "fill":
            try:
                taken = await self.repo.order_id_booked(oid)
            except Exception:
                logger.warning(
                    "[%s] could not check whether order id %s is booked",
                    signal.strategy_name, oid, exc_info=True,
                )
                taken = False
            if not taken:
                return oid, signal.reason
            why = f"the fill matched was order {oid}'s, already booked"
            oid = str(uuid.uuid4())
        else:
            why = "no fill of its own found, @ mid"
        logger.warning(
            "[%s] %s %s filled after its order timed out and is booked "
            "ESTIMATED @ %s (%s)", signal.strategy_name, signal.action.value,
            signal.symbol, order.filled_price, why,
        )
        return oid, f"ESTIMATED {signal.reason} (filled after a timeout; {why})"

    def _unfilled(self, symbol: str, requested: float, filled: float) -> float:
        """What a close left unfilled, in whole szDecimals steps; 0.0 when
        it filled all it sent. Measured against the request rounded as
        the wrapper rounds it (to nearest), so rounding — a legacy
        unrounded row, float noise like 0.3 - 0.1 — or an over-fill is no
        remainder. The rest stays on the row for the strategy's next exit
        to close, instead of reconcile finding it without a row."""
        if filled >= requested:
            return 0.0
        p = self.exchange.get_size_precision(symbol)
        return max(0.0, round(round(requested, p) - filled, p))

    async def _open_refusal(
        self,
        signal: Signal,
        wanted_side: str,
        size: float,
        current_price: float,
        leverage: int,
    ) -> tuple[int, str, str] | None:
        """Why this OPEN must not be sent, as `(log level, category,
        message)`, or None when it may go ahead. The category is a stable
        name for the dedup latch; the message carries the live numbers.

        Reads only — nothing here writes or orders, which is what makes
        it safe to run before a flip's synthesized close. The signalling
        strategy's own row on this coin is ignored throughout: it is
        either absent (plain open) or the position the flip is about to
        close.
        """
        open_rows: list = []
        if self.repo:
            open_rows = await self.repo.get_open_positions()
        others = [p for p in open_rows if p.strategy_name != signal.strategy_name]
        same_coin = [p for p in others if p.symbol == signal.symbol]

        # Cross-strategy coin gate + family gate (allow_multi_coin=False).
        # Every open row is read, not `get_open_position_any`'s single
        # arbitrary row: with two holders that row could be the strategy's
        # own and hide the other one. Without BotControl the flag reads as
        # its default, False — what BotControl itself answers without Redis.
        if self.repo:
            allow_multi = (
                await self.control.get_allow_multi_coin() if self.control else False
            )
            if not allow_multi:
                if same_coin:
                    holders = ", ".join(sorted(
                        f"{p.strategy_name} ({p.side})" for p in same_coin
                    ))
                    return logging.INFO, "coin held", (
                        f"{holders} already holds {signal.symbol} "
                        f"(allow_multi_coin=False)"
                    )

                # Family-level correlation guard: strategies tagged with
                # the same `family` are near-identical ports of the same
                # signal math (cdc_macd and macd_zero are both the
                # EMA12/26 cross ≡ MACD-zero-cross on 1d). With
                # allow_multi_coin disabled, at most ONE strategy per
                # family may hold a position — across ALL coins — because
                # on different coins the two entries would still be
                # effectively the same trade in duplicate. Strategies
                # whose name is unknown to the registry (family=None)
                # keep the legacy per-coin behaviour only.
                family = get_strategy_family(signal.strategy_name)
                if family:
                    for held in others:
                        if get_strategy_family(held.strategy_name) != family:
                            continue
                        return logging.INFO, "family exposed", (
                            f"family '{family}' already exposed via "
                            f"{held.strategy_name} on {held.symbol} "
                            f"(allow_multi_coin=False)"
                        )

        # Opposite side on the same coin — refused whatever
        # allow_multi_coin says (the flag only allows same-side
        # stacking). HL nets a long and a short on one coin into one
        # position, and the post-trade parity check compares SIGNED nets,
        # so a long 1.0 + short 1.0 in the DB against a flat exchange
        # "passes" while each strategy manages a position that does not
        # exist on its own.
        opposite = [p for p in same_coin if p.side != wanted_side]
        if opposite:
            holders = ", ".join(sorted(
                f"{p.strategy_name} ({p.side})" for p in opposite
            ))
            return logging.WARNING, "opposite side", (
                f"{holders} holds the opposite side of {signal.symbol}; "
                f"HL would net the two into one position"
            )

        # Total exposure cap, in NOTIONAL on both sides: every open row's
        # `size × entry_price` plus this order's `size × price`.
        # `PositionRecord` has no leverage column, so the old
        # `getattr(p, "leverage", 1)` always read 1 — open rows counted
        # at full notional while the new order counted notional/leverage,
        # two units in one sum (analysis-2026-09-15.md § 4). Notional is
        # the unit both sides actually have.
        cap = settings.max_total_exposure_usd
        if self.repo and cap > 0:
            current_notional = sum(
                float(p.size) * float(p.entry_price)
                for p in open_rows
                if not (
                    p.strategy_name == signal.strategy_name
                    and p.symbol == signal.symbol
                )
            )
            new_notional = float(size) * float(current_price)
            projected = current_notional + new_notional
            if projected > cap:
                return logging.WARNING, "exposure cap", (
                    f"total exposure cap would be exceeded: "
                    f"${current_notional:,.0f} open notional + "
                    f"${new_notional:,.0f} new = ${projected:,.0f} > "
                    f"${cap:,.0f} MAX_TOTAL_EXPOSURE_USD"
                )

        # Audit H8 (2026-05-10): hard ceiling on `signal.size` overrides.
        # Strategies that emit `Signal(size=400)` (vvv_hedge) bypass
        # `_calculate_size` entirely → MAX_POSITION_SIZE_USD doesn't bind.
        # An accidental param bump (holding_vvv 400 → 4000) silently
        # produces a 10× position with the same hard SL.
        #
        # We cap in MARGIN terms (PR #32 review fix). MAX_POSITION_SIZE_USD
        # is the margin cap on regular signals; `_calculate_size` scales
        # notional by leverage but margin stays at MAX_POSITION_SIZE_USD.
        # To keep units consistent, the H8 ceiling on a sized signal is
        # `multiplier × MAX_POSITION_SIZE_USD` of MARGIN — i.e. a sized
        # open at 10× leverage gets 10× the notional, same as regular
        # signals do. `signal.size is not None` (not truthy) so a
        # `Signal(size=0)` — a strategy bug, not a legitimate "use
        # default" — goes through the cap check too.
        #
        # Safety-cap rejection is warning-only and NOT Telegram-routed:
        # the cap firing means the safety system worked, not that the bot
        # is broken. Emitting ErrorOccurred here spammed Telegram on every
        # legitimate cap-block. Example: vvv_hedge emits Signal(size=400).
        # At VVV $7 the notional is 400 × 7 = $2,800. Margin = notional /
        # leverage: at the strategy's default leverage=2 that's $1,400
        # (under the $2,000 cap → passes), but at leverage=1 it's $2,800
        # (over cap → rejected, warning logged, no Telegram).
        if signal.size is not None:
            lev = max(int(leverage or 1), 1)
            max_margin = (
                settings.signal_size_max_multiplier
                * settings.max_position_size_usd
            )
            requested_notional = float(size) * float(current_price)
            requested_margin = requested_notional / lev
            if requested_margin > max_margin:
                return logging.WARNING, "size ceiling", (
                    f"Signal(size={signal.size}) margin "
                    f"${requested_margin:.0f} (notional "
                    f"${requested_notional:.0f} / {lev}x) exceeds the "
                    f"{settings.signal_size_max_multiplier:.0f}x safety "
                    f"ceiling ${max_margin:.0f} (SIGNAL_SIZE_MAX_MULTIPLIER "
                    f"× MAX_POSITION_SIZE_USD)"
                )

        return None

    async def _note_refusal(
        self,
        signal: Signal,
        category: str,
        bar_time,
        message: str,
        *,
        publish: bool = False,
        level: int = logging.WARNING,
    ) -> None:
        """Log (and optionally publish) a refusal once per (category, bar).

        An edge-triggered strategy re-syncs or resets after a refusal and
        re-emits the same signal on every tick until its bar closes, so
        the same refusal repeats once a minute. It is news when its
        category differs from the last one noted for this (strategy,
        coin), or when it happens on a different bar. The category, not
        the message, is the key, because messages carry live numbers (the
        exposure cap's dollar amounts) that never repeat exactly. Without
        a bar time (a caller outside the tick loop) a note expires after
        `_REFUSAL_NOTE_TTL_SECONDS`. Repeats log at DEBUG and are never
        published.
        """
        key = (signal.strategy_name, signal.symbol)
        now = time.monotonic()
        prev = self._refusal_notes.get(key)
        if prev is not None:
            p_category, p_bar, p_at = prev
            if (
                p_category == category
                and p_bar == bar_time
                and (
                    bar_time is not None
                    or now - p_at < self._REFUSAL_NOTE_TTL_SECONDS
                )
            ):
                logger.debug(
                    "[%s] %s on %s refused again (%s): %s",
                    signal.strategy_name, signal.action.value,
                    signal.symbol, category, message,
                )
                return
        self._refusal_notes[key] = (category, bar_time, now)
        logger.log(level, "[%s] %s", signal.strategy_name, message)
        if publish:
            await self._publish_error(signal.strategy_name, message)

    async def _flat_after_refused_open(
        self,
        signal: Signal,
        category: str,
        bar_time,
        message: str,
        *,
        publish: bool = False,
        level: int = logging.WARNING,
    ) -> None:
        """A plain OPEN (no position to flip) refused before anything was
        sent: nothing was opened, so the strategy is reset to flat.

        Strategies switch their in-memory state to the side they asked for
        when they emit the OPEN. Left like that the strategy believed in a
        position the DB never got, ran SL/TP for it, and — for an
        edge-triggered entry — never re-entered. The flip counterpart is
        `_close_half_of_refused_flip`. Noted once per (category,
        bar); the reset itself logs at DEBUG, because a standing-condition
        strategy re-emits the refused OPEN every tick.
        """
        await self._note_refusal(
            signal, category, bar_time,
            f"{message}; strategy reset to flat",
            publish=publish, level=level,
        )
        await self._reset_strategy_state(
            signal.strategy_name,
            why=f"its {signal.action.value} {signal.symbol} was refused "
                f"({category})",
            quiet=True,
        )

    def _resync_strategy_to_row(self, row) -> None:
        """Point a strategy back at an open DB row it still owns, taking
        back the signal it just made. Never raises. It is re-armed with
        its state from before that signal when it held one (NU-5a): a
        ratcheted trailing stop keeps its ratchet and a due exit fires
        again. Else the row's open-time `state_json`, as at startup."""
        strat = next(
            (s for s in self.strategies if s.name == row.strategy_name), None,
        )
        if strat is None:
            return
        state = self._pre_signal_state.get(row.strategy_name)
        try:
            if state is None:
                self._restore_strategy_from_row(strat, row)
            else:
                strat.restore_from_json(row.side, row.entry_price, dict(state))
        except Exception:
            logger.exception(
                "[%s] re-sync to the kept %s position failed",
                row.strategy_name, row.side,
            )

    async def _close_half_of_refused_flip(
        self, signal: Signal, existing, current_price: float, leverage: int,
        bar_time, category: str, head: str, *, tail: str = "",
        publish: bool = False, level: int = logging.WARNING,
    ) -> None:
        """A flip whose open half a gate refused: the close half goes out
        anyway, and the strategy ends flat.

        Closing only reduces exposure, and the flip is the only exit
        hash_supertrend and kalman_breakout have — it used to be refused
        whole by any gate but a risk limit (exposure cap, coin held,
        family, size ceiling, a failed leverage push or gate read), which
        kept the position past its exit. Exits are never blocked; the open
        half is what the gate protects. A close that does not go through
        is handled by `_flip_close` (re-armed, the close the engine's).
        """
        if not await self._flip_close(
            signal, existing, current_price, leverage, bar_time,
            why=f"(the open half is refused: {category})",
        ):
            return
        await self._reset_strategy_state(
            signal.strategy_name,
            why=f"a flip closed its position but {category} refused the open",
        )
        await self._note_refusal(
            signal, category, bar_time,
            f"{head} — closed the {existing.side} {signal.symbol} half of a "
            f"flip but blocked {signal.action.value}; flat, strategy reset "
            f"to flat{tail}",
            publish=publish, level=level,
        )

    async def _open_unchecked(self, signal: Signal, wanted_side: str) -> None:
        """An OPEN whose own-position read raised: nothing was sent. The
        strategy switched to `wanted_side` when it emitted the signal, so
        it is reset to flat. If it held a position before (a flip), that
        position still has to close: the engine owes it the close of the
        other side from the next tick, which ends quietly if there is no
        such row."""
        name = signal.strategy_name
        held_before = name in self._pre_signal_state
        await self._reset_strategy_state(
            name, why=f"its {signal.action.value} {signal.symbol} was not "
                      "sent: its position could not be read",
        )
        if not held_before:
            return
        old = "short" if wanted_side == "long" else "long"
        await self._pend_close(
            (name, signal.symbol), old, f"flip to {wanted_side} ({signal.reason[:60]})",
        )
        logger.warning(
            "[%s] the %s→%s flip on %s could not read its position: the open "
            "is not sent, and the engine closes the %s from the next tick",
            name, old, wanted_side, signal.symbol, old,
        )

    async def _close_raised(self, signal: Signal, side: str, row, exc) -> None:
        """A close raised before its outcome was known (a DB read, the
        order call). The strategy dropped the position when it emitted the
        exit; the close becomes the engine's and goes again next tick, and
        the strategy is re-armed on its row when the row can be read."""
        if self.repo is None:
            return  # no row to owe a close to (no DB: not production)
        key = (signal.strategy_name, signal.symbol)
        new = key not in self._pending_closes
        await self._pend_close(key, side, signal.reason)
        if row is None:
            try:
                row = await self.repo.get_open_position(*key)
            except Exception:
                row = None
        if row is not None and row.side == side:
            self._resync_strategy_to_row(row)
            how = "re-armed on its row"
        else:
            how = "not re-armed (its row could not be read)"
        # The tick handler logs the exception itself, every time.
        logger.log(
            logging.WARNING if new else logging.INFO,
            "[%s] %s %s raised %s: %s — the close is the engine's now and "
            "goes again next tick; strategy %s", signal.strategy_name,
            signal.action.value, signal.symbol, type(exc).__name__, exc, how,
        )

    async def _flip_close(
        self,
        signal: Signal,
        existing,
        current_price: float,
        leverage: int,
        bar_time,
        why: str,
    ) -> bool:
        """Close `existing` through the standard close path (DB-driven
        size, trade record, parity) as the first half of a flip. Returns
        whether the close fully succeeded and nothing is left on the
        exchange for the open to land on.

        On failure the strategy is brought back in line with what the DB
        now says: still holding the old position (the close did not fill)
        → re-armed on it, the close the engine's; row closed (the close
        filled but parity did not verify) → reset to flat. Either way it no
        longer believes in the side it asked for. An exception from the
        close is re-raised after that, so the tick handler still classifies
        it (transient outage vs bug); `TradeDbDivergence` passes straight
        through (the bot is already paused and the incident published).
        """
        wanted = "long" if signal.action == SignalAction.OPEN_LONG else "short"
        close_action = (
            SignalAction.CLOSE_SHORT if existing.side == "short"
            else SignalAction.CLOSE_LONG
        )
        logger.info(
            "[%s] Flip detected — closing %s %s %s (reason: %s)",
            signal.strategy_name, existing.side, signal.symbol, why,
            signal.reason[:80],
        )
        flip_close = Signal(
            action=close_action,
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            reason=f"Auto-close before flip ({signal.reason[:60]})",
        )
        key = (signal.strategy_name, signal.symbol)
        self._flip_closes[key] = None
        try:
            ok = await self._execute_signal(flip_close, current_price, leverage)
        except TradeDbDivergence:
            raise
        except Exception:
            await self._align_strategy_after_failed_flip_close(signal, existing)
            raise
        finally:
            settled = self._flip_closes.pop(key, None)
        if settled == "external-held":
            # The old side was gone and is booked, but the exchange holds
            # the other side with no row: an open would stack onto it.
            # `_book_external_close` reset the strategy and said so.
            logger.warning(
                "[%s] Flip %s→%s on %s: open not sent — the exchange holds "
                "the other side without a row", signal.strategy_name,
                existing.side, wanted, signal.symbol,
            )
            return False
        if ok or settled == "external":
            # "external": the old side was gone and is now booked (NU-5a),
            # and nothing is held, so the open goes ahead.
            return True

        # ABORT: opening a new opposite-direction position while the close
        # failed leaves DB+exchange divergent (audit H1, 2026-05-09). The
        # reconcile loop will eventually catch the leftover, but we mustn't
        # actively make it worse here.
        if settled is None:
            outcome = await self._align_strategy_after_failed_flip_close(
                signal, existing,
            )
        else:  # `_close_left_open` has re-armed the strategy already
            outcome = (
                f"the {existing.side} is still open and the engine closes it; "
                "strategy re-armed on it"
            )
        await self._note_refusal(
            signal, "flip close failed", bar_time,
            f"Flip-close failed on {signal.symbol} "
            f"({existing.side}→{wanted}); open aborted to keep DB and "
            f"exchange consistent — {outcome}.",
            # A close `_close_left_open` classified has its own alerts, one
            # per (strategy, coin) episode; repeating it here on every new
            # bar was one Telegram per bar for as long as it stayed open.
            publish=settled is None,
        )
        return False

    async def _align_strategy_after_failed_flip_close(
        self, signal: Signal, existing,
    ) -> str:
        """Re-read the strategy's row after a flip-close that did not
        succeed and align the strategy with it. Returns what was done."""
        try:
            row = await self.repo.get_open_position(
                signal.strategy_name, signal.symbol,
            )
        except Exception:
            logger.warning(
                "[%s] flip-close failed and the position re-read failed "
                "too — assuming the %s is still open",
                signal.strategy_name, existing.side, exc_info=True,
            )
            row = existing
        if row is not None and row.side == existing.side:
            self._resync_strategy_to_row(row)
            return f"the {existing.side} is still open; strategy re-synced to it"
        await self._reset_strategy_state(
            signal.strategy_name,
            why="its flip-close did not verify but the row is closed",
        )
        return (
            f"the {existing.side} row is closed but the close did not verify "
            f"cleanly; strategy reset to flat"
        )

    async def _flat_after_flip_open_failed(
        self, signal: Signal, detail: str, bar_time, *, publish: bool,
    ) -> None:
        """A flip's close went through but its open did not: the book is
        flat for this strategy, so the strategy is reset to flat instead of
        believing in the side it asked for."""
        await self._reset_strategy_state(
            signal.strategy_name,
            why="a flip closed its position but the open failed",
        )
        message = (
            f"Flip on {signal.symbol}: the old position was closed but the "
            f"new {signal.action.value} failed ({detail}). Flat; strategy "
            f"reset to flat."
        )
        if publish:
            await self._note_refusal(
                signal, "flip open failed", bar_time, message, publish=True,
            )
        else:
            logger.warning("[%s] %s", signal.strategy_name, message)

    async def _check_parity_after_trade(self, symbol: str) -> bool:
        """Verify exchange position for `symbol` matches DB sum across
        all open strategies. Lightweight — only fires on the trade event,
        not every tick.

        Triggers an alert (logged + Telegram via ErrorOccurred) when
        |db_net_size - exchange_net_size| > tolerance. Tolerance is the
        MIN of two per-coin bounds (audit M4 + tightened in H4):

          step_bound  = 10 × szDecimals minimum step (rounding floor):
              BTC szDecimals=5 → 1e-4
              ETH szDecimals=4 → 1e-3
              SOL szDecimals=2 → 1e-1
          ratio_bound = 0.5% × max(|db_net|, |ex_net|) (proportional):
              catches partial-fill drift on small positions where
              step_bound alone would be too loose (0.1 SOL = $15 of
              drift on a $200 position = 7.5% silent error).

        When both sides are zero (legitimate "both flat"), only the
        step_bound applies — ratio_bound = 0 would tolerate nothing.
        Default fallback (unknown coin) is szDecimals=4 → step 1e-3.

        Action: alert (Telegram via ErrorOccurred); ALSO returns False
        so the caller can refuse to proceed (e.g. flip-detect must not
        open opposite side if the close left exchange diverged). Returns
        True when in-tolerance (the happy path). Returns True on internal
        exceptions too (don't block trade flow on parity-check failures
        — those go through the existing exception path in the caller).
        """
        if self.repo is None:
            return True

        # DB side: signed-net for the symbol across all open strategies.
        # Long contributes +size, short contributes -size — engine's
        # netting model matches HL's per-coin position.
        db_positions = await self.repo.get_open_positions()
        db_net = 0.0
        for p in db_positions:
            if p.symbol != symbol:
                continue
            db_net += p.size if p.side == "long" else -p.size

        try:
            ex_positions = await self.exchange.get_positions()
        except ExchangeReadError as e:
            # Deliberately non-blocking: a read failure must not stall
            # the trade flow. But say plainly that parity was NOT
            # verified — the True below is "don't pause", not "checked
            # and fine".
            logger.warning(
                "[%s] parity NOT VERIFIED for %s — exchange read failed "
                "(%s). Proceeding without a parity check; the next "
                "reconcile pass re-checks it.",
                settings.exchange_mode, symbol, e,
            )
            return True
        except Exception:
            logger.exception(
                "parity NOT VERIFIED for %s — exchange.get_positions() "
                "raised; proceeding without a parity check", symbol,
            )
            return True  # don't block trade flow when we can't read exchange
        ex_net = 0.0
        for p in ex_positions:
            if p.symbol == symbol:
                ex_net = p.size if p.side == "long" else -p.size
                break

        diff = abs(db_net - ex_net)
        # Tolerance combines two bounds (audit H4, 2026-05-10):
        #   step_bound  = 10× exchange minimum step (catches HL rounding)
        #   ratio_bound = 0.5% of expected size (catches partial-fill drift
        #                 on small positions, where the szDecimals bound
        #                 alone is way too loose: SOL szDecimals=2 →
        #                 0.1 SOL ≈ $15 of drift slips through; on a $200
        #                 position that's a 7.5% silent error)
        # We take the MIN so the looser bound never wins. The expected
        # size is `max(|db_net|, |ex_net|)` — using both means a partial
        # close that left exchange at 0 still gets a meaningful bound
        # against the DB's recorded size.
        sz_decimals = self.exchange.get_size_precision(symbol)
        step_bound = 10 * (10 ** -sz_decimals)
        expected_size = max(abs(db_net), abs(ex_net))
        ratio_bound = 0.005 * expected_size  # 0.5%
        # If both sides are zero, only step_bound applies.
        tolerance = (
            min(step_bound, ratio_bound) if expected_size > 0 else step_bound
        )
        if diff <= tolerance:
            return True

        msg = (
            f"PARITY MISMATCH on {symbol}: DB net {db_net:.6f} vs "
            f"exchange {ex_net:.6f} (diff {diff:.6f})"
        )
        logger.warning("[%s] %s", settings.exchange_mode, msg)
        if self.event_bus:
            try:
                await self.event_bus.publish(
                    ErrorOccurred(
                        strategy=f"parity/{symbol}",
                        message=(
                            f"{msg}. Investigate before next trade — likely "
                            f"partial-fill drift or netting bug."
                        ),
                    )
                )
            except Exception:
                logger.exception("parity: event publish failed")
        return False

    async def _check_trade_rate_anomalies(self) -> None:
        """Detect spam-trading strategies and auto-pause via Redis.

        Triggers when EITHER:
          (a) hourly trade count > baseline_multiplier × 7d-avg-per-hour
              AND > min_hourly_floor (prevents 5×0=0 false-pass on quiet
              strategies)
          (b) hourly trade count > absolute_ceiling regardless of baseline
              (catches first-time-active strategies whose 7d avg is 0)

        Once a strategy trips, it goes into self._rate_alarm_paused for
        the remainder of this process — we don't re-check it (the operator
        must manually re-enable via /options or SREM after investigating).
        Without this dedupe the alarm would fire on every check interval
        as long as the spam burst is still in the 1h window.
        """
        from datetime import datetime, timedelta, timezone

        if self.repo is None or self.control is None:
            return

        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)
        seven_days_ago = now - timedelta(days=7)

        hourly = await self.repo.get_trade_counts_per_strategy(one_hour_ago)
        if not hourly:
            return  # nothing to evaluate
        weekly = await self.repo.get_trade_counts_per_strategy(seven_days_ago)

        already_disabled = await self.control.get_disabled_strategies()

        floor = settings.trade_rate_alarm_min_hourly_floor
        ceiling = settings.trade_rate_alarm_absolute_ceiling
        mult = settings.trade_rate_alarm_baseline_multiplier

        for strategy_name, hourly_count in hourly.items():
            if strategy_name in self._rate_alarm_paused:
                continue
            if strategy_name in already_disabled:
                continue

            baseline_per_hour = weekly.get(strategy_name, 0) / (24 * 7)
            spike = (
                hourly_count > mult * baseline_per_hour
                and hourly_count > floor
            )
            ceiling_breach = hourly_count > ceiling

            if not (spike or ceiling_breach):
                continue

            reason_bits = []
            if spike:
                reason_bits.append(
                    f"{hourly_count}/h vs 7d-baseline {baseline_per_hour:.2f}/h "
                    f"({mult}× threshold)"
                )
            if ceiling_breach:
                reason_bits.append(f"absolute ceiling {ceiling}/h breached")
            why = "; ".join(reason_bits)

            try:
                await self.control.disable_strategy(strategy_name)
            except Exception:
                logger.exception(
                    "rate-alarm: disable_strategy failed for %s", strategy_name,
                )
                continue

            self._rate_alarm_paused.add(strategy_name)
            logger.warning(
                "rate-alarm: AUTO-PAUSED %s on %s — %s",
                strategy_name, settings.exchange_mode, why,
            )

            # Route to Telegram via the existing error-event channel.
            if self.event_bus:
                try:
                    await self.event_bus.publish(
                        ErrorOccurred(
                            strategy=strategy_name,
                            message=(
                                f"AUTO-PAUSED ({settings.exchange_mode}): "
                                f"trade-rate spike — {why}. Strategy disabled "
                                f"via Redis; investigate before re-enabling."
                            ),
                        )
                    )
                except Exception:
                    logger.exception("rate-alarm: event publish failed")

    async def _run_side_services(self) -> None:
        """HODL evaluation (every 6h) and the vault scanner (daily), on the
        services-owner bot only (`SERVICES_OWNER`, roadmap NU-7).

        Exactly one bot per tenant runs them. HODL inputs (price, RSI,
        on-chain levels) are mode-agnostic, so running them in every
        container sent three identical verdict-change messages; every bot
        shares one Postgres, so a second vault scanner would repeat the
        14 MB catalogue fetch and send `vault.qualified` twice for one
        state change. Vault data is HL mainnet data whichever bot fetches
        it. The owner used to be mainnet; it is now whichever mode the
        dashboard names (default paper), so stopping the real-money bot no
        longer silences them.
        """
        if not settings.services_owner:
            return
        now = time.time()
        if (now - self._last_hodl_check) > 6 * 3600:
            try:
                await self._evaluate_hodl_signals()
            except Exception:
                logger.exception("HODL signal evaluation failed")
            self._last_hodl_check = time.time()
        # On failure we DON'T advance _last_vault_poll, so a transient HL
        # outage retries on the next tick instead of waiting a full day.
        if self.repo and (now - self._last_vault_poll) > 24 * 3600:
            try:
                await self._poll_vaults()
                self._last_vault_poll = time.time()
            except Exception:
                logger.exception("Vault scan failed — will retry on next tick")

    async def _evaluate_hodl_signals(self) -> None:
        """Run all HODL signals; on verdict change vs last tick, push a Telegram
        notification (if configured). HODL signals are advisory-only — they
        never trade. We just want a heads-up when conditions shift.
        """
        from hypertrade.hodl.registry import all_signals, load_all
        load_all()
        for sig in all_signals():
            try:
                state = await sig.evaluate()
            except Exception:
                logger.exception("HODL signal %s failed", sig.name)
                continue

            prev = self._last_hodl_zones.get(sig.name)
            self._last_hodl_zones[sig.name] = state.verdict

            if prev is None or prev == state.verdict:
                continue

            # Recovery / inter-transient noise filter (Copilot review fix
            # on PR #98): suppress when prev was a transient sentinel
            # AND the new verdict is either normal (recovery — symmetrical
            # to the never-notified failure) or another transient (e.g.
            # "Unknown — evaluation failed" → "Unknown — no data" — still
            # broken, just a different shape, no point pinging twice).
            #
            # Failure transitions in the OTHER direction (normal → transient)
            # still publish below — the user wants to know when something
            # newly breaks.
            if _is_transient_unknown_verdict(prev):
                logger.info(
                    "[hodl/%s] verdict transition from transient "
                    "(suppressed notify): %r → %r (score %.2f)",
                    sig.name, prev, state.verdict, state.score,
                )
                continue

            # Verdict changed → emit info event so Telegram forwards it
            # without an ⚠️ ERROR prefix. A new Unknown-shaped verdict
            # IS published — the user wants to know when something just
            # broke (the prev==Unknown short-circuit above prevents
            # double-publishing on consecutive failures).
            if self.event_bus:
                try:
                    await self.event_bus.publish(
                        HodlVerdictChanged(
                            strategy=f"hodl/{sig.name}",
                            asset=sig.asset,
                            prev_verdict=prev,
                            new_verdict=state.verdict,
                        )
                    )
                except Exception:
                    logger.exception("HODL notify publish failed")

            logger.info(
                "[hodl/%s] verdict change: %r → %r (score %.2f)",
                sig.name, prev, state.verdict, state.score,
            )

    async def _poll_vaults(self) -> None:
        """Run the daily HyperLiquid vault scanner. Lazy-init the poller
        so we don't pay the import cost when scanning is disabled.
        """
        from hypertrade.vaults.poller import VaultPoller

        if self._vault_poller is None:
            self._vault_poller = VaultPoller(
                repo=self.repo,
                event_bus=self.event_bus,
                track_user_address=settings.effective_vault_tracking_address,
            )
        result = await self._vault_poller.poll()
        logger.info("vault scan result: %s", result)

    async def _poll_funding(self) -> None:
        """Store HL funding events since the newest stored one in
        funding_payments. Paging, attribution and the event key are
        `engine/funding.py`'s, shared with the backfill CLI."""
        from datetime import datetime, timedelta, timezone
        if self.repo is None:
            return
        latest = await self.repo.get_latest_funding_timestamp()
        # On first run, look back 24h. Otherwise from the latest stored ts
        # minus 1 minute (overlap window — the event key dedupes it).
        if latest is None:
            start = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            start = funding.as_utc(latest) - timedelta(minutes=1)

        result = await funding.ingest_funding(
            self.repo,
            self.exchange.get_user_funding_history,
            funding.to_ms(start),
            max_pages=funding.POLL_MAX_PAGES,
        )
        if result.new:
            logger.info(
                "Funding poll: %d new payment(s) recorded (%+.4f USDC, %d unattributed)",
                result.new, result.new_usdc, result.unattributed_new,
            )
        if not result.complete:
            logger.info(
                "Funding poll: stopped after %d page(s) at %s; the next poll continues",
                result.pages, result.newest,
            )

    async def _resolve_close_size(
        self, strategy_name: str, symbol: str, expected_side: str
    ) -> tuple[float | None, object | None]:
        """Determine how much to close on an exit signal, and the open DB
        row it closes (None without a DB).

        Source of truth: the strategy's own DB position. Falls back to the
        exchange position only if DB is unavailable.

        Returns None if no position should be closed (logs a warning).
        When the answer is "there is no position at all", the strategy is
        also reset to flat, in memory and in its snapshot: it believed it
        held something, and the DB (or, without a DB, the exchange) is the
        source of truth. Left alone it kept that belief — and with it the
        refusal to re-enter — until the next restart restored it again.

        NU-5a: the close goes out reduce-only, so no size opens or flips
        anything. A flat or opposite exchange is sent the DB size and
        refuses it (`_close_left_open` classifies that); an unreadable one
        is sent the DB size too.
        """
        if self.repo:
            db_pos = await self.repo.get_open_position(strategy_name, symbol)
            if db_pos is None:
                logger.warning(
                    "[%s] CLOSE_%s ignored for %s — no open DB position",
                    strategy_name, expected_side.upper(), symbol,
                )
                await self._reset_strategy_state(
                    strategy_name,
                    why=f"its CLOSE_{expected_side.upper()} {symbol} found "
                        f"no open DB position",
                )
                return None, None
            if db_pos.side != expected_side:
                logger.warning(
                    "[%s] CLOSE_%s ignored for %s — DB position side is %s",
                    strategy_name, expected_side.upper(), symbol, db_pos.side,
                )
                return None, None

            # Sanity-clamp to the exchange's actual netted position size for
            # this side. If the DB thinks we own more than the exchange has
            # (could happen after partial reconcile), close only what exists.
            try:
                ex_pos = await self.exchange.get_position(symbol)
            except Exception as e:
                # The clamp is a safety net, not the source of truth —
                # whatever failed, we fall back to the DB size and say
                # so. One handler; the type is in the message.
                logger.warning(
                    "[%s] CLOSE_%s for %s — exchange read failed (%s: %s); "
                    "closing the DB size unclamped, reduce-only",
                    strategy_name, expected_side.upper(), symbol,
                    type(e).__name__, e,
                )
                return db_pos.size, db_pos
            held = _held_on_side(ex_pos, expected_side)
            if held > 1e-9 and db_pos.size > held + 1e-9:
                logger.warning(
                    "[%s] CLOSE_%s clamping DB size %.6f to exchange %.6f for %s",
                    strategy_name, expected_side.upper(),
                    db_pos.size, held, symbol,
                )
                return held, db_pos
            return db_pos.size, db_pos

        # No DB available — fall back to exchange total (legacy behavior).
        # A read failure here must NOT read as "no position": that would
        # silently drop a close signal. Raise so the tick's handler sees
        # it (and routes a transient one into the outage aggregator).
        try:
            ex_pos = await self.exchange.get_position(symbol)
        except ExchangeReadError:
            logger.warning(
                "[%s] CLOSE_%s for %s — no DB and the exchange read "
                "failed; cannot determine close size",
                strategy_name, expected_side.upper(), symbol,
            )
            raise
        if not (ex_pos and ex_pos.side == expected_side):
            logger.warning(
                "[%s] CLOSE_%s ignored for %s — no matching exchange position",
                strategy_name, expected_side.upper(), symbol,
            )
            await self._reset_strategy_state(
                strategy_name,
                why=f"its CLOSE_{expected_side.upper()} {symbol} found no "
                    f"matching exchange position (no DB)",
            )
            return None, None
        return ex_pos.size, None

    async def _close_left_open(
        self, signal: Signal, order, row, *, filled_at: float | None = None,
    ) -> str | None:
        """NU-5a: a close left `row` open — all of it (`order` rejected or
        zero-filled) or the rest (filled short at `filled_at`: `order`
        None). A confirmed read (`_held_after_close`) says which:

        a. Nothing left on the row's side: it was closed outside this bot
           (liquidation, by hand, a late fill of a timed-out order). The
           row is booked from fills; nothing to retry, one notice. The
           rest of a close that filled, which no fill covers, is size
           drift instead: booked at `filled_at`, pnl 0, logged only. A row
           that cannot be priced at all stays open for reconcile, the
           strategy flat: the exchange holds nothing for it to manage.
        b. Anything else, unreadable included: still open. The close
           becomes the engine's (`_PendingClose`): sent every tick until
           it goes through, whatever the strategy's rule says by then. The
           strategy, which dropped the position, is re-armed with its
           pre-signal state. One alert per (strategy, coin) episode
           (`_close_missed`).

        Returns "external" (booked, the exchange flat), "external-held"
        (booked, the exchange on the other side with no row), "alerted",
        "retry", or None if the row could not be classified or booked;
        a flip in flight gets it too. Never raises: a failure still
        re-arms the strategy on `row` and keeps the close the engine's.
        """
        key = (signal.strategy_name, signal.symbol)
        got = (
            "filled short" if order is None
            else "came back zero-filled" if order.status.value == "filled"
            else f"came back {order.status.value}"
            + (f" ({order.error})" if getattr(order, "error", None) else "")
        )
        if not self.repo or row is None:
            logger.warning(
                "[%s] %s %s %s, with no DB row to hold it against",
                signal.strategy_name, signal.action.value, signal.symbol, got,
            )
            return None
        outcome: str | None = "retry"
        try:
            if order is None:  # what is left is the rest on the row
                row = await self.repo.get_open_position(*key) or row
            held, pos, exchange = await self._held_after_close(row.symbol, row.side)
            if not held:
                booked = await self._book_external_close(
                    row, f"its {signal.action.value} {got} and the "
                    f"exchange is {exchange}", pos,
                    drift_price=filled_at if order is None else None,
                )
                outcome = (
                    None if not booked
                    else "external" if pos is None else "external-held"
                )
                if not booked and self._pending_closes.pop(key, None):
                    # Nothing is left to send it to; reconcile books the row.
                    logger.info(
                        "[%s] its pending close on %s ends: the exchange "
                        "holds none of it", signal.strategy_name, signal.symbol,
                    )
            else:
                self._resync_strategy_to_row(row)
                outcome = await self._close_missed(
                    signal, row, f"{got}; exchange: {exchange}",
                    rejected=order is not None,
                )
        except Exception:
            logger.exception(
                "[%s] %s %s left its row open and classifying why failed — "
                "strategy re-armed on its row, the close the engine's",
                signal.strategy_name, signal.action.value, signal.symbol,
            )
            self._resync_strategy_to_row(row)
            await self._pend_close(key, row.side, signal.reason)
        if key in self._flip_closes:
            self._flip_closes[key] = outcome
        return outcome

    async def _held_after_close(self, symbol: str, side: str) -> tuple[bool, object, str]:
        """Whether the exchange still holds any `side` of `symbol` (an
        unreadable one does), the position it read (None when flat or
        unreadable), and what, as text. "Nothing" needs a second read to
        agree CLOSE_CONFIRM_DELAY_SECONDS later (reconcile's rule)."""
        pos = None
        for attempt in (1, 2):
            if attempt == 2:
                await asyncio.sleep(self.CLOSE_CONFIRM_DELAY_SECONDS)
            try:
                pos = await self.exchange.get_position(symbol)
            except Exception as e:
                return True, None, f"unreadable ({type(e).__name__}: {e})"
            if _held_on_side(pos, side) > 1e-9:
                return True, pos, _exchange_text(pos)
        if pos is not None and float(pos.size) <= 1e-9:
            pos = None
        return False, pos, _exchange_text(pos)

    async def _pend_close(self, key: tuple[str, str], side: str, reason: str) -> _PendingClose:
        """The close the engine owes `key`'s `side` row, made so if it is
        not yet, and noted on the row so a restart sends it again. Never
        raises; a note that cannot be written is logged — a restart before
        the close goes through would then forget it."""
        pending = self._pending_closes.get(key)
        if pending is not None and pending.side == side:
            return pending
        pending = self._pending_closes[key] = _PendingClose(side=side, reason=reason)
        saved = False
        if self.repo is not None:
            try:
                saved = await self.repo.mark_pending_close(
                    key[0], key[1], side, {
                        "reason": reason[:200],
                        "since": datetime.now(timezone.utc).isoformat(
                            timespec="seconds",
                        ),
                    },
                )
            except Exception as e:
                logger.warning(
                    "[%s] noting the pending close on its row failed (%s: %s)",
                    key[0], type(e).__name__, e,
                )
        if not saved:
            logger.warning(
                "[%s] the close the engine owes its %s %s is not saved on the "
                "row: a restart before it goes through forgets it",
                key[0], side, key[1],
            )
        return pending

    async def _close_missed(
        self, signal: Signal, row, detail: str, *, rejected: bool,
    ) -> str:
        """Count one more attempt that left `row` open (rejected, filled
        zero, or — `rejected=False` — filled short) and write its one log
        line. The first rejection alerts; from CLOSE_REJECTS_BEFORE_WIDE_BAND
        misses on every attempt gets WIDE_CLOSE_BAND, and the first of
        those that misses alerts critical. Each alert goes out once per
        episode (one undelivered goes with the next miss); every miss logs
        one line — an alert's text at ERROR the first time, everything
        else at INFO, but for the first miss of an episode that does not
        alert, at WARNING. "alerted" if an alert went out now."""
        key = (signal.strategy_name, signal.symbol)
        pending = await self._pend_close(key, row.side, signal.reason)
        pending.misses += 1
        n = pending.misses
        wide = n > CLOSE_REJECTS_BEFORE_WIDE_BAND
        level = 2 if wide else 1 if rejected else 0
        what = (
            f"{signal.action.value} {signal.symbol} ({row.side} {row.size}) "
            f"{detail}"
        )
        if key in self._flip_closes:
            what += "; the close half of a flip, so its open is not sent"
        if level == 0 or pending.alerted >= level:
            logger.log(
                logging.WARNING if n == 1 else logging.INFO,
                "[%s] %s — attempt %d (%s IOC band) left the row open; the "
                "engine sends the close again next tick",
                signal.strategy_name, what, n,
                f"{WIDE_CLOSE_BAND * 100:.0f}%" if wide else "default",
            )
            return "retry"
        if level == 2:
            message = (
                f"CRITICAL: {what} — {n} closes in a row left the position "
                f"open, the last with a {WIDE_CLOSE_BAND * 100:.0f}% IOC band. "
                "The bot keeps sending it every tick; close it by hand."
            )
        else:
            message = (
                f"{what}. The position is still open: the strategy is "
                "re-armed with its state from before the exit, and the close "
                "is sent again every tick until it goes through, whatever "
                "the strategy's rule says by then."
            )
        logger.log(
            logging.ERROR if pending.logged < level else logging.INFO,
            "[%s] %s", signal.strategy_name, message,
        )
        pending.logged = max(pending.logged, level)
        if not await self._publish_error(signal.strategy_name, message):
            return "retry"
        pending.alerted = level
        return "alerted"

    async def _book_external_close(
        self, row, why: str, other=None, *, drift_price: float | None = None,
    ) -> bool:
        """NU-5a class (a): book `row` as closed outside this bot, priced
        from fills as reconcile prices an orphan (#167), with its PnL and
        one notice; reset the strategy, unless a flip's open goes ahead —
        only when the exchange is flat: `other`, a position on the other
        side that no row owns, would take the open stacked onto it.
        `drift_price`: `row` is the rest of a close that filled at that
        price, and one no fill covers is size drift — booked there with
        pnl 0 and logged, not published (`close_row_externally`).
        Returns whether it was booked (an unpriceable row stays open)."""
        try:
            priced = await self.repo.close_row_externally(
                self.exchange, row, drift_price=drift_price,
            )
        except Exception:
            logger.exception(
                "[%s] booking the external close of %s %s failed — the "
                "periodic reconcile retries it", row.strategy_name,
                row.side, row.symbol,
            )
            return False
        if priced is None:
            logger.warning(
                "[%s] %s %s %s: %s, but the row could not be priced (or was "
                "already closed) — left to the periodic reconcile",
                row.strategy_name, row.side, row.size, row.symbol, why,
            )
            return False
        key = (row.strategy_name, row.symbol)
        flip = key in self._flip_closes
        opens = flip and other is None
        self._pending_closes.pop(key, None)
        await self._on_reconcile_close(row.strategy_name, priced.pnl, reset=not opens)
        if opens:
            after = "The flip's open goes ahead."
        elif other is not None:
            after = (
                f"The exchange holds {_exchange_text(other)} {row.symbol} that "
                "no row owns: "
                + ("the flip's open is not sent, and " if flip else "")
                + "reconcile decides on it."
            )
        else:
            after = "Nothing to retry."
        if priced.drift:
            # Bookkeeping, not an event: no money moved (double-close-2).
            logger.warning(
                "[%s] Size drift: %s %s %s — %s, and no fill closed it: the "
                "row held more than the exchange did. Booked @ %.6f (the "
                "close's fill), pnl 0, no fee. %s", row.strategy_name,
                row.side, row.size, row.symbol, why, priced.exit_price, after,
            )
            return True
        message = (
            f"External close: {row.side} {row.size} {row.symbol} — {why}. "
            f"None of the {row.side} is left on the exchange (a liquidation, "
            "a close by hand, or a late fill of this bot's own order); booked "
            f"@ {priced.exit_price:.6f} "
            f"({'ESTIMATED @ mid' if priced.estimated else 'fill'}), "
            f"pnl {priced.pnl:+.4f}. {after}"
        )
        logger.warning("[%s] %s", row.strategy_name, message)
        await self._publish_error(row.strategy_name, message)
        return True

    async def _ensure_leverage_pushed(self, symbol: str) -> bool:
        """Push per-coin leverage to the exchange if it's drifted from
        what we last pushed (audit H1). Returns whether the exchange is
        known to hold the target leverage.

        The target is the max `s.leverage` across all strategies trading
        ``symbol`` — matching the startup-time push in `main.py`, whose
        accepted values seed `_pushed_leverage`. Only actually calls the
        exchange when the target differs from `_pushed_leverage[symbol]`,
        so the cost is one dict lookup per OPEN tick in the steady state.

        A push that raises or is rejected returns False and the caller
        aborts the open. It used to be logged and the open went ahead at
        whatever leverage HL had: `_calculate_size` sizes the notional
        for the NEW leverage, so on a bump HL margins it at the old one —
        the liquidation path audit H1 exists to close, one trade late
        (`analysis-2026-09-15.md` § 4). The failure is published once per
        (coin, target) until a push for that coin succeeds; the target is
        not cached, so the next OPEN retries.
        """
        target = max(
            (s.leverage for s in self.strategies if s.symbol == symbol),
            default=1,
        )
        target = max(int(target), 1)
        previous = self._pushed_leverage.get(symbol)
        if previous == target:
            return True
        try:
            ok = await self.exchange.update_leverage(
                symbol, target, is_cross=True,
            )
            failure = None if ok else "rejected by the exchange"
        except Exception as e:
            logger.warning(
                "Leverage push for %s (target %dx) raised",
                symbol, target, exc_info=True,
            )
            failure = f"{type(e).__name__}: {e}"
        if failure is None:
            self._pushed_leverage[symbol] = target
            self._leverage_push_alerted.pop(symbol, None)
            logger.info(
                "Re-pushed %s leverage=%dx (was %s)",
                symbol, target,
                f"{previous}x" if previous is not None else "unset",
            )
            return True

        message = (
            f"Leverage push for {symbol} to {target}x failed ({failure}); "
            f"last accepted: "
            f"{f'{previous}x' if previous is not None else 'none this run'}. "
            f"Opens on {symbol} are refused until a push succeeds."
        )
        logger.warning("%s", message)
        if self._leverage_push_alerted.get(symbol) != target:
            self._leverage_push_alerted[symbol] = target
            await self._publish_error(f"leverage/{symbol}", message)
        return False

    def _calculate_size(self, price: float, leverage: int = 1) -> float:
        """Calculate position size in base units.

        Notional = MAX_POSITION_SIZE_USD * leverage. Margin used = MAX_POSITION_SIZE_USD.
        So a $200 max position at 5x means $1000 notional exposure with $200 of margin.
        """
        if price <= 0:
            return 0.0
        notional = settings.max_position_size_usd * min(max(1, int(leverage)), 50)
        return round(notional / price, 6)
