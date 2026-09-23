"""Strategy engine runner — the core loop."""

import asyncio
import logging
import socket
import time
from dataclasses import dataclass

import aiohttp

from hypertrade.config import settings
from hypertrade.data.feed import fetch_candles
from hypertrade.db.repo import ReconcileResult, Repository
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


class EngineRunner:
    # How long a refusal note stands when the caller gave no bar time.
    _REFUSAL_NOTE_TTL_SECONDS = 3600.0

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

    async def _publish_error(self, strategy: str, message: str) -> None:
        """Publish an ErrorOccurred; a publish failure is logged, never raised."""
        if not self.event_bus:
            return
        try:
            await self.event_bus.publish(
                ErrorOccurred(strategy=strategy, message=message)
            )
        except Exception:
            logger.exception("%s: event publish failed", strategy)

    def _restore_strategy_from_row(self, strat: Strategy, pos) -> None:
        """Restore one strategy's in-memory position state from its DB row.

        Prefers the exact `state_json` captured at open time; falls back
        to recompute-based `restore_state` when it was never persisted
        (legacy rows or strategies that don't override export_state).
        """
        import json
        state = None
        if pos.state_json:
            try:
                state = json.loads(pos.state_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "[%s] Invalid state_json — falling back to recompute",
                    pos.strategy_name,
                )
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

    async def _restore_strategies(self, positions: list) -> None:
        """Restore every strategy from the open DB rows (plus the Redis
        cooldown snapshot for strategies that are flat)."""
        strat_by_name = {s.name: s for s in self.strategies}
        restored = 0
        unmanaged: list[str] = []
        for pos in positions:
            strat = strat_by_name.get(pos.strategy_name)
            if strat is None:
                # The strategy is not running in this bot (allowlist,
                # mainnet opt-in, a strategy cap). Nothing will manage this
                # position: no SL, no exit signal.
                logger.warning(
                    "State restore: open %s %s row of %s has no running "
                    "strategy — the position is UNMANAGED",
                    pos.symbol, pos.side, pos.strategy_name,
                )
                unmanaged.append(f"{pos.strategy_name} {pos.symbol} {pos.side}")
                continue
            self._restore_strategy_from_row(strat, pos)
            restored += 1
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
            logger.info(
                "Reconcile: bot is paused — dry run. Divergences are "
                "reported; no rows closed, no trades written, no orders.",
            )

        result = await self.repo.reconcile_positions(
            self.exchange,
            close_exchange_orphans=not paused,
            on_strategy_close=self._on_reconcile_close,
            dry_run=paused,
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
        if notice is None:
            self._last_reconcile_notice = None
        elif notice == self._last_reconcile_notice:
            logger.info(
                "Reconcile: same outcome as the previous pass — not "
                "re-publishing to Telegram (%s)", notice,
            )
        elif self.event_bus:
            self._last_reconcile_notice = notice
            try:
                await self.event_bus.publish(
                    ErrorOccurred(
                        strategy="reconcile",
                        message=f"{label} reconcile — {notice}",
                    )
                )
            except Exception:
                logger.exception("Reconcile: event publish failed")
        else:
            self._last_reconcile_notice = notice
        return result

    async def _on_reconcile_close(
        self, strategy_name: str, pnl: float | None = None,
    ) -> None:
        """Reconcile closed a DB row outside the normal signal path.

        Resets the strategy's in-memory state AND books the realised PnL
        into the portfolio. Before this, reconcile closes were invisible
        to `portfolio.record_pnl`, so the `MAX_DAILY_LOSS_USD` kill
        switch never saw the losses it exists to stop.
        """
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

    async def _reset_strategy_state(self, strategy_name: str, why: str) -> None:
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
        """
        for s in self.strategies:
            if s.name == strategy_name:
                try:
                    s.reset_position()
                except Exception:
                    logger.exception("reset_position failed for %s", strategy_name)
                    return
                logger.info(
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

        # Periodic reconcile every 5 minutes. Catches positions closed
        # manually on the exchange, partial fills, and any post-startup
        # divergence between DB and exchange.
        if self.repo and (time.time() - self._last_reconcile) > 300:
            try:
                await self._run_reconcile("Periodic")
            except Exception:
                logger.exception("Periodic reconcile failed")
            self._last_reconcile = time.time()

        # Funding poll every 30 minutes. Backfills user_funding_history
        # since the most-recent stored payment (or 24h back on first run).
        if self.repo and (time.time() - self._last_funding_poll) > 1800:
            try:
                await self._poll_funding()
            except Exception:
                logger.exception("Funding poll failed")
            self._last_funding_poll = time.time()

        # HODL signal evaluation every 6h, owned by the mainnet bot only.
        # Same rationale as the vault scanner gate below: HODL inputs
        # (price, RSI, on-chain levels) are mode-agnostic, so running in
        # every container produced 3× duplicate Telegram notifications
        # ("MAINNET / PAPER / TESTNET ... HODL hype_accumulation") on each
        # verdict change. Mainnet is the canonical owner — co-located with
        # Telegram (PR #114) and vault scanner (PR #113).
        if (
            settings.exchange_mode == "mainnet"
            and (time.time() - self._last_hodl_check) > 6 * 3600
        ):
            try:
                await self._evaluate_hodl_signals()
            except Exception:
                logger.exception("HODL signal evaluation failed")
            self._last_hodl_check = time.time()

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

        # Vault scanner: daily poll, owned by the mainnet bot only. All
        # bot containers share the same Postgres, so running it in every
        # container would duplicate the 14 MB catalogue fetch and risk
        # emitting two `vault.qualified` alerts for the same state
        # change. Vaults are an on-chain mainnet concept (testnet/paper
        # have no real vaults) and the /vaults dashboard is pinned to
        # mainnet, so mainnet is the natural single owner. The published
        # event's `mode` field tags Telegram messages as MAINNET via
        # EventBus.publish. The /vaults dashboard reads from the shared
        # DB so the row is visible from any mode the user browses.
        # On failure we DON'T advance _last_vault_poll, so a transient HL
        # outage retries on the next tick instead of waiting a full day.
        if (
            self.repo
            and settings.exchange_mode == "mainnet"
            and (time.time() - self._last_vault_poll) > 24 * 3600
        ):
            try:
                await self._poll_vaults()
                self._last_vault_poll = time.time()
            except Exception:
                logger.exception(
                    "Vault scan failed — will retry on next tick"
                )

        # Honor flat-all request before everything else. The request is
        # acknowledged ONLY on full success: acknowledging a flat-all
        # that never read the exchange (or left closes failing) tells
        # the operator the book is flat when it is not.
        paused_by_flat_all = False
        if self.control:
            pending = await self.control.get_pending_flat_request()
            if pending:
                status = await self._flat_all_positions()
                if status.ok:
                    await self._pause_after_flat_all(status)
                    paused_by_flat_all = True
                    await self.control.acknowledge_flat_request(pending)
                else:
                    logger.error(
                        "Flat-all NOT acknowledged (%s) — the request stays "
                        "pending and retries next tick", status.reason,
                    )
                    if self.event_bus:
                        try:
                            await self.event_bus.publish(
                                ErrorOccurred(
                                    strategy="flat-all",
                                    message=(
                                        f"Flat-all did not complete: "
                                        f"{status.reason}. Positions may "
                                        f"still be open; the request stays "
                                        f"pending and retries next tick."
                                    ),
                                )
                            )
                        except Exception:
                            logger.exception("Flat-all: event publish failed")

        paused = False
        disabled: set[str] = set()
        leverage_overrides: dict[str, int] = {}
        # Per-tenant mainnet allowlist (layer 2). Empty set = nothing
        # trades on mainnet for this tenant. Re-read every tick so UI
        # toggles take effect without a bot restart. Outside mainnet,
        # `mainnet_enabled` stays None and the filter is skipped.
        mainnet_enabled: set[str] | None = None
        if self.control:
            paused = await self.control.is_paused()
            disabled = await self.control.get_disabled_strategies()
            leverage_overrides = await self.control.get_all_leverage_overrides()
            if settings.is_mainnet and settings.tenant_id:
                try:
                    mainnet_enabled = (
                        await self.control.get_mainnet_enabled_strategies_for_tenant(
                            settings.tenant_id,
                        )
                    )
                except Exception:
                    logger.exception(
                        "Failed to read tenant mainnet allowlist — "
                        "fail-closed: no strategies trade this tick"
                    )
                    mainnet_enabled = set()
            # Apply overrides to strategy instances (used for sizing this tick)
            for s in self.strategies:
                if s.name in leverage_overrides:
                    s.leverage = leverage_overrides[s.name]

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

        if paused:
            logger.info("Tick skipped — bot is paused")
        else:
            active = filter_strategies_for_tick(
                self.strategies, disabled, mainnet_enabled,
            )
            logger.info(
                "Tick started — evaluating %d/%d strategies (disabled: %s)",
                len(active),
                len(self.strategies),
                sorted(disabled) if disabled else "none",
            )
            for strategy in active:
                try:
                    await self._run_strategy(strategy)
                except TradeDbDivergence:
                    # Already handled at source: bot paused, detailed
                    # ErrorOccurred event published, full traceback
                    # logged via logger.exception. Skip the generic
                    # "Strategy tick failed" branch so we don't
                    # double-publish + double-log the same incident
                    # (Copilot review fix on PR #107).
                    continue
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
        """Close every open position with a market order.

        Returns a status instead of None so the caller can refuse to
        acknowledge the request. `ok` is true only when the exchange was
        actually readable AND every close filled — "I could not read the
        exchange" used to look identical to "there was nothing to close".
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

        if not positions:
            logger.info("Flat-all requested — no open positions")
            return FlatAllStatus(ok=True, closed=0)

        logger.warning("Flat-all closing %d positions", len(positions))
        failed = 0
        closed = 0
        for pos in positions:
            try:
                close_side = "sell" if pos.side == "long" else "buy"
                # Audit H5+H6 + PR #31 review: get EVERY open DB row for
                # this coin (not just one — `allow_multi_coin=True` lets
                # multiple strategies hold rows on the same coin). The
                # exchange shows one netted position; we split the close
                # fee/PnL across all open rows by size weight so each
                # strategy's PnL is recorded with its own entry_price.
                # Fixed in PR #31 review: previously closed only one row,
                # leaving siblings to be orphan-closed at PnL=0 by
                # reconcile.
                db_recs = (
                    await self.repo.get_open_positions_for_symbol(pos.symbol)
                    if self.repo else []
                )

                order = await self.exchange.place_order(
                    pos.symbol, close_side, pos.size, OrderType.MARKET
                )
                if order.status.value != "filled":
                    logger.error(
                        "Flat-all: order NOT filled for %s — position may still be open",
                        pos.symbol,
                    )
                    failed += 1
                    continue

                filled_price = order.filled_price or 0
                if not db_recs:
                    # No DB-side open record (rare exchange-side orphan).
                    # Best-effort: record one trade for history with the
                    # exchange's full size, using its VWAP entry as the
                    # only entry signal we have.
                    fee = filled_price * pos.size * settings.taker_fee_rate
                    pnl = realized_pnl(
                        side=pos.side, entry_price=pos.entry_price,
                        exit_price=filled_price, size=pos.size, fee=fee,
                    )
                    if self.repo:
                        await self.repo.record_trade(
                            order_id=order.id,
                            strategy_name="manual_flat",
                            symbol=pos.symbol,
                            side=order.side,
                            size=pos.size,
                            price=filled_price,
                            fee=fee,
                            pnl=pnl,
                            reason="Flat-all from dashboard (no open DB rec)",
                        )
                    await self.portfolio.record_pnl(pnl)
                    closed += 1
                    logger.warning(
                        "Closed %s %s @ %.2f (no DB rec; PnL %.2f)",
                        pos.side, pos.symbol, filled_price, pnl,
                    )
                    continue

                # Split close fee + PnL across each open DB row by its
                # share of total open size. Per-row PnL uses that row's
                # own entry_price (audit H6) — exchange VWAP would be
                # wrong when two strategies opened at different prices.
                total_db_size = sum(float(r.size) for r in db_recs) or 1.0
                for rec in db_recs:
                    share = float(rec.size) / total_db_size
                    rec_size = float(rec.size)
                    rec_fee = filled_price * rec_size * settings.taker_fee_rate
                    rec_entry = float(rec.entry_price)
                    rec_pnl = realized_pnl(
                        side=rec.side, entry_price=rec_entry,
                        exit_price=filled_price, size=rec_size, fee=rec_fee,
                    )
                    # Audit H5: single atomic trade+close per row.
                    await self.repo.record_trade_and_close_position(
                        order_id=order.id,
                        strategy_name=rec.strategy_name,
                        symbol=pos.symbol,
                        trade_side=order.side,
                        size=rec_size,
                        price=filled_price,
                        fee=rec_fee,
                        pnl=rec_pnl,
                        reason="Flat-all from dashboard",
                    )
                    await self.portfolio.record_pnl(rec_pnl)
                    logger.warning(
                        "Closed [%s] %s %s @ %.2f (entry %.2f, size %.6f, share %.0f%%, PnL %.2f)",
                        rec.strategy_name, rec.side, pos.symbol,
                        filled_price, rec_entry, rec_size, share * 100, rec_pnl,
                    )
                    # The strategy did not ask for this close; without the
                    # reset it keeps believing in the position and never
                    # re-enters.
                    await self._reset_strategy_state(
                        rec.strategy_name, why="flat-all closed its DB position",
                    )
                closed += 1
            except Exception:
                logger.exception("Failed to close position %s", pos.symbol)
                failed += 1

        if failed:
            logger.error(
                "Flat-all completed with %d failures — manual intervention may be required",
                failed,
            )
            return FlatAllStatus(
                ok=False,
                closed=closed,
                failed=failed,
                reason=f"{failed} of {len(positions)} close(s) failed",
            )
        return FlatAllStatus(ok=True, closed=closed)

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

    async def _run_strategy(self, strategy: Strategy) -> None:
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
        signal = await strategy.on_candle(closed_candles)
        signal_action = "none"
        signal_reason = ""

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

            bar_time = (
                closed_candles["timestamp"].iloc[-1]
                if "timestamp" in closed_candles.columns else None
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
        ceiling, and the leverage push — runs before the synthesized close,
        so a refused open never closes the old position and frees the coin
        for another strategy mid-tick (`analysis-2026-09-15.md` § 4). A
        risk-limit refusal (kill switch, daily-loss cap) is the exception:
        it refuses only the open half and still lets the close through,
        because closing reduces risk and a flip is the only exit some
        strategies have. Whatever happens, the strategy ends up believing
        in the position the DB actually holds.

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
            existing = await self.repo.get_open_position(
                signal.strategy_name, signal.symbol
            )
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
            if existing is None:
                await self._note_refusal(
                    signal, "risk limit", bar_time,
                    f"Risk limit breached — execution of "
                    f"{signal.action.value} {signal.symbol} blocked",
                    publish=True,
                )
                return False
            if await self._flip_close(
                signal, existing, current_price, leverage, bar_time,
                why="(the open half is refused by a risk limit)",
            ):
                await self._reset_strategy_state(
                    signal.strategy_name,
                    why="a flip closed its position but a risk limit "
                        "refused the open",
                )
                await self._note_refusal(
                    signal, "risk limit", bar_time,
                    f"Risk limit breached — closed the {existing.side} "
                    f"{signal.symbol} half of a flip but blocked "
                    f"{signal.action.value}; flat, strategy reset to flat",
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
            # refuses the open — and, before a flip's close, the whole flip.
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
                    await self._keep_position_after_refused_flip(
                        signal, existing, category, reason, bar_time,
                    )
                else:
                    logger.log(
                        level, "[%s] Skipping %s %s — %s",
                        signal.strategy_name, signal.action.value,
                        signal.symbol, reason,
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
        elif signal.action == SignalAction.CLOSE_LONG:
            close_size = await self._resolve_close_size(
                signal.strategy_name, signal.symbol, "long"
            )
            if close_size is None:
                return False
            order = await self.exchange.place_order(
                signal.symbol, "sell", close_size, OrderType.MARKET
            )
            size = close_size
        elif signal.action == SignalAction.CLOSE_SHORT:
            close_size = await self._resolve_close_size(
                signal.strategy_name, signal.symbol, "short"
            )
            if close_size is None:
                return False
            order = await self.exchange.place_order(
                signal.symbol, "buy", close_size, OrderType.MARKET
            )
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

        if order.status.value != "filled":
            logger.warning("Order not filled: %s", order.status)
            if flipped:
                await self._flat_after_flip_open_failed(
                    signal, f"order {order.status.value}", bar_time,
                    publish=True,
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
        # § 4). A close that fills short still closes the whole DB row —
        # partial-fill handling for closes is a separate follow-up.
        if abs(order.size - size) > 1e-12:
            logger.info(
                "[%s] %s %s filled %s (requested %s)",
                signal.strategy_name, signal.action.value, signal.symbol,
                order.size, size,
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
                    await self.repo.record_trade_and_close_position(
                        order_id=order.id,
                        strategy_name=signal.strategy_name,
                        symbol=signal.symbol,
                        trade_side=order.side,
                        size=size,
                        price=filled_price,
                        fee=fee,
                        pnl=close_pnl or 0,
                        reason=signal.reason,
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

        return parity_ok

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
        logger.warning("[%s] %s", signal.strategy_name, message)
        if publish:
            await self._publish_error(signal.strategy_name, message)

    def _resync_strategy_to_row(self, row) -> None:
        """Point a strategy back at an open DB row it still owns (same
        restore logic as startup). Never raises."""
        strat = next(
            (s for s in self.strategies if s.name == row.strategy_name), None,
        )
        if strat is None:
            return
        try:
            self._restore_strategy_from_row(strat, row)
        except Exception:
            logger.exception(
                "[%s] re-sync to the kept %s position failed",
                row.strategy_name, row.side,
            )

    async def _keep_position_after_refused_flip(
        self, signal: Signal, existing, category: str, reason: str, bar_time,
    ) -> None:
        """A flip whose open was refused before anything was sent: nothing
        was closed.

        Notes the refusal once per (category, bar) and points the strategy
        back at the position it still holds. It switched its in-memory
        state to the side it asked for when it emitted the signal; left
        like that it would run SL/TP for a position that does not exist
        while the real one went unmanaged.
        """
        wanted = "long" if signal.action == SignalAction.OPEN_LONG else "short"
        await self._note_refusal(
            signal, category, bar_time,
            f"Flip {existing.side}→{wanted} on {signal.symbol} refused — "
            f"{reason}. Nothing closed: keeping the open {existing.side} "
            f"(size {existing.size} @ {existing.entry_price}) and re-syncing "
            f"the strategy to it",
        )
        self._resync_strategy_to_row(existing)

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
        whether the close fully succeeded.

        On failure the strategy is brought back in line with what the DB
        now says: still holding the old position (the close did not fill)
        → re-synced to it; row closed (the close filled but parity did not
        verify) → reset to flat. Either way it no longer believes in the
        side it asked for. An exception from the close is re-raised after
        that, so the tick handler still classifies it (transient outage vs
        bug); `TradeDbDivergence` passes straight through (the bot is
        already paused and the incident published).
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
        try:
            if await self._execute_signal(flip_close, current_price, leverage):
                return True
        except TradeDbDivergence:
            raise
        except Exception:
            await self._align_strategy_after_failed_flip_close(signal, existing)
            raise

        # ABORT: opening a new opposite-direction position while the close
        # failed leaves DB+exchange divergent (audit H1, 2026-05-09). The
        # reconcile loop will eventually catch the leftover, but we mustn't
        # actively make it worse here.
        outcome = await self._align_strategy_after_failed_flip_close(
            signal, existing,
        )
        await self._note_refusal(
            signal, "flip close failed", bar_time,
            f"Flip-close failed on {signal.symbol} "
            f"({existing.side}→{wanted}); open aborted to keep DB and "
            f"exchange consistent — {outcome}.",
            publish=True,
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
        """Pull HL funding events since the latest stored timestamp and
        upsert them into the funding_payments table. Best-effort attribution
        to the strategy that holds the coin at funding time."""
        from datetime import datetime, timedelta, timezone
        if self.repo is None:
            return
        latest = await self.repo.get_latest_funding_timestamp()
        # On first run, look back 24h. Otherwise from the latest stored ts
        # minus 1 minute (overlap window — dedup by hash handles duplicates).
        if latest is None:
            start_ms = int(
                (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
            )
        else:
            start_ms = int((latest - timedelta(minutes=1)).timestamp() * 1000)

        try:
            events = await self.exchange.get_user_funding_history(start_ms)
        except Exception:
            logger.exception("Funding fetch failed")
            return

        if not events:
            return

        inserted = 0
        for ev in events:
            try:
                ts_ms = int(ev.get("time", 0))
                h = str(ev.get("hash", ""))
                delta = ev.get("delta", {})
                coin = str(delta.get("coin", ""))
                usdc = float(delta.get("usdc", 0))
                szi = delta.get("szi")
                szi_f = float(szi) if szi is not None else None
                fr = delta.get("fundingRate")
                fr_f = float(fr) if fr is not None else None
                if not h or not coin:
                    continue

                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

                # Best-effort strategy attribution: open position on this coin
                strat_name = None
                pos = await self.repo.get_open_position_any(coin)
                if pos is not None:
                    strat_name = pos.strategy_name

                ok = await self.repo.upsert_funding_payment(
                    ts=ts, h=h, coin=coin, usdc=usdc,
                    szi=szi_f, funding_rate=fr_f, strategy_name=strat_name,
                )
                if ok:
                    inserted += 1
            except Exception:
                logger.exception("Failed to record funding event %s", ev)

        if inserted:
            logger.info("Funding poll: %d new payment(s) recorded", inserted)

    async def _resolve_close_size(
        self, strategy_name: str, symbol: str, expected_side: str
    ) -> float | None:
        """Determine how much to close on an exit signal.

        Source of truth: the strategy's own DB position. Falls back to the
        exchange position only if DB is unavailable.

        Returns None if no position should be closed (logs a warning).
        When the answer is "there is no position at all", the strategy is
        also reset to flat, in memory and in its snapshot: it believed it
        held something, and the DB (or, without a DB, the exchange) is the
        source of truth. Left alone it kept that belief — and with it the
        refusal to re-enter — until the next restart restored it again.
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
                return None
            if db_pos.side != expected_side:
                logger.warning(
                    "[%s] CLOSE_%s ignored for %s — DB position side is %s",
                    strategy_name, expected_side.upper(), symbol, db_pos.side,
                )
                return None

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
                    "closing the DB size unclamped",
                    strategy_name, expected_side.upper(), symbol,
                    type(e).__name__, e,
                )
                ex_pos = None
            if ex_pos is not None and ex_pos.side == expected_side:
                if db_pos.size > ex_pos.size + 1e-9:
                    logger.warning(
                        "[%s] CLOSE_%s clamping DB size %.6f to exchange %.6f for %s",
                        strategy_name, expected_side.upper(),
                        db_pos.size, ex_pos.size, symbol,
                    )
                    return ex_pos.size
            return db_pos.size

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
            return None
        return ex_pos.size

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
