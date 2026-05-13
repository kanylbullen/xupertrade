"""Strategy engine runner — the core loop."""

import asyncio
import logging
import socket
import time

import aiohttp

from hypertrade.config import settings
from hypertrade.data.feed import fetch_candles
from hypertrade.db.repo import Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.portfolio import PortfolioManager
from hypertrade.engine.signals import Signal, SignalAction
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
from hypertrade.exchange.base import Exchange, OrderType
from hypertrade.strategies.base import Strategy

logger = logging.getLogger(__name__)


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


def _is_transient_network_error(exc: BaseException) -> bool:
    """True for HL/network outages where the bot recovers on its own."""
    if isinstance(exc, _TRANSIENT_NETWORK_ERRORS):
        return True
    # HL SDK ServerError uses a single class for all 4xx/5xx — only
    # the 5xx + 408/429 are transient (4xx validation/auth aren't).
    try:
        from hyperliquid.utils.error import ServerError  # noqa: PLC0415
        if isinstance(exc, ServerError):
            msg = str(exc)
            return any(c in msg for c in ("502", "503", "504", "408", "429"))
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
    def __init__(
        self,
        exchange: Exchange,
        strategies: list[Strategy],
        repo: Repository | None = None,
        event_bus: EventBus | None = None,
        control: BotControl | None = None,
    ) -> None:
        self.exchange = exchange
        self.strategies = strategies
        self.repo = repo
        self.event_bus = event_bus
        self.control = control
        self.portfolio = PortfolioManager(exchange, control=control)
        self._last_reconcile = 0.0  # epoch seconds
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
        self._pushed_leverage: dict[str, int] = {}

    async def startup(self) -> None:
        """Restore in-memory strategy state from DB after a restart, then
        kick off a background re-push of the persisted Caddy TLS config
        so HTTPS comes back up without requiring a manual click on the
        Options page. The TLS push is deliberately fire-and-forget so a
        slow/unreachable Caddy can't delay engine startup.
        """
        if not self.repo:
            self._kick_caddy_tls_restore()
            return
        try:
            positions = await self.repo.get_open_positions()
        except Exception:
            logger.exception("Failed to fetch open positions for state restoration")
            self._kick_caddy_tls_restore()
            return

        import json
        strat_by_name = {s.name: s for s in self.strategies}
        restored = 0
        for pos in positions:
            strat = strat_by_name.get(pos.strategy_name)
            if strat is None:
                continue
            # Prefer exact restore from stored state_json. Fall back to
            # recompute-based restore_state if state was never persisted
            # (legacy rows or strategies that don't override export_state).
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
                    restored += 1
                    continue
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
            restored += 1

        # Audit M6 follow-up: also restore Redis-backed strategy state for
        # strategies that are FLAT but might be in cooldown. The
        # position-table state_json only covers in-position windows; the
        # Redis snapshot covers the post-close cooldown window. Without
        # this, a restart inside the cooldown window would reset the
        # counter and let the strategy immediately re-enter on the same
        # stale bar.
        if self.control:
            for strat in self.strategies:
                if strat.name in {p.strategy_name for p in positions}:
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
                # Strategy is flat but had persisted cooldown state.
                # restore_from_json supports the "side defensively reset
                # if not long" path — pass the sentinel "flat" so any
                # strategy that doesn't expect non-{long,short} side
                # falls back cleanly.
                try:
                    strat.restore_from_json("flat", 0.0, redis_state)
                    logger.info(
                        "Restored %s cooldown state from Redis: %s",
                        strat.name, redis_state,
                    )
                except Exception:
                    logger.exception(
                        "restore_from_json failed for %s redis state %s",
                        strat.name, redis_state,
                    )

        if positions:
            logger.info(
                "State restoration complete: %d/%d positions restored",
                restored, len(positions),
            )

    def _reset_strategy_state(self, strategy_name: str) -> None:
        """Called by reconcile when it closes a DB position outside the
        normal signal path. Without this, the strategy keeps _in_position=True
        in RAM while DB shows closed → no re-entry possible, and on next
        restart the strategy reads no open position → re-enters duplicately.
        """
        for s in self.strategies:
            if s.name == strategy_name:
                try:
                    s.reset_state()
                    logger.info(
                        "Reset in-memory state for %s (DB position was orphan-closed)",
                        strategy_name,
                    )
                except Exception:
                    logger.exception("reset_state failed for %s", strategy_name)
                return

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
                actions = await self.repo.reconcile_positions(
                    self.exchange,
                    on_strategy_close=self._reset_strategy_state,
                )
                if actions:
                    logger.warning(
                        "Periodic reconcile: %d action(s) — %s",
                        len(actions), "; ".join(actions),
                    )
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

        # HODL signal evaluation every 6h. Notify Telegram on verdict change
        # (e.g. green→yellow, yellow→red). Only the testnet bot has Telegram
        # configured, so other modes evaluate silently.
        if (time.time() - self._last_hodl_check) > 6 * 3600:
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

        # Vault scanner: daily poll, owned by the testnet bot only. Both
        # paper and testnet share the same Postgres in the default Compose
        # setup, so running it in every container would duplicate the
        # 14 MB catalogue fetch and risk emitting two `vault.qualified`
        # alerts for the same state change. Telegram also lives on the
        # testnet bot, so this puts the alerts at the same source as the
        # poll. The /vaults dashboard reads from the shared DB regardless
        # of which mode the user is browsing.
        # On failure we DON'T advance _last_vault_poll, so a transient HL
        # outage retries on the next tick instead of waiting a full day.
        if (
            self.repo
            and settings.exchange_mode == "testnet"
            and (time.time() - self._last_vault_poll) > 24 * 3600
        ):
            try:
                await self._poll_vaults()
                self._last_vault_poll = time.time()
            except Exception:
                logger.exception(
                    "Vault scan failed — will retry on next tick"
                )

        # Honor flat-all request before everything else
        if self.control:
            pending = await self.control.get_pending_flat_request()
            if pending:
                await self._flat_all_positions()
                await self.control.acknowledge_flat_request(pending)

        paused = False
        disabled: set[str] = set()
        leverage_overrides: dict[str, int] = {}
        if self.control:
            paused = await self.control.is_paused()
            disabled = await self.control.get_disabled_strategies()
            leverage_overrides = await self.control.get_all_leverage_overrides()
            # Apply overrides to strategy instances (used for sizing this tick)
            for s in self.strategies:
                if s.name in leverage_overrides:
                    s.leverage = leverage_overrides[s.name]

        if paused:
            logger.info("Tick skipped — bot is paused")
        else:
            active = [s for s in self.strategies if s.name not in disabled]
            logger.info(
                "Tick started — evaluating %d/%d strategies (disabled: %s)",
                len(active),
                len(self.strategies),
                sorted(disabled) if disabled else "none",
            )
            for strategy in active:
                try:
                    await self._run_strategy(strategy)
                except Exception as e:
                    logger.exception("Error running strategy %s", strategy.name)
                    # Skip Telegram alert for transient HL/network errors
                    # — bot recovers on next tick automatically and we
                    # don't want to flood during outages (CLAUDE.md backlog
                    # Open-Low: "Suppress Telegram noise on transient
                    # HL-fetch failures"). Heartbeat staleness is the
                    # signal for monitoring real outages.
                    if (
                        self.event_bus
                        and not _is_transient_network_error(e)
                    ):
                        await self.event_bus.publish(
                            ErrorOccurred(
                                strategy=strategy.name,
                                message="Strategy tick failed",
                            )
                        )

        # Update unrealized P&L for open positions (always, even when paused)
        if self.repo:
            try:
                await self._update_position_pnl()
            except Exception:
                logger.exception("Failed to update position P&L")

        # Snapshot equity (always, even when paused)
        try:
            balance = await self.exchange.get_balance()
            if self.repo:
                await self.repo.snapshot_equity(
                    balance.total, balance.available, balance.unrealized_pnl
                )

            if self.event_bus:
                positions = await self.exchange.get_positions()
                await self.event_bus.publish(
                    BotHeartbeat(
                        mode=settings.exchange_mode,
                        strategies=",".join(s.name for s in self.strategies),
                        equity=balance.total,
                        positions=len(positions),
                        uptime_seconds=int(time.time() - _start_time),
                    )
                )
        except Exception:
            logger.exception("Failed to snapshot equity")

    async def _flat_all_positions(self) -> None:
        """Close every open position with a market order."""
        try:
            positions = await self.exchange.get_positions()
        except Exception:
            logger.exception("Failed to fetch positions for flat-all")
            return

        if not positions:
            logger.info("Flat-all requested — no open positions")
            return

        logger.warning("Flat-all closing %d positions", len(positions))
        failed = 0
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
                    if pos.side == "long":
                        realized_pnl = (filled_price - pos.entry_price) * pos.size - fee
                    else:
                        realized_pnl = (pos.entry_price - filled_price) * pos.size - fee
                    if self.repo:
                        await self.repo.record_trade(
                            order_id=order.id,
                            strategy_name="manual_flat",
                            symbol=pos.symbol,
                            side=order.side,
                            size=pos.size,
                            price=filled_price,
                            fee=fee,
                            pnl=realized_pnl,
                            reason="Flat-all from dashboard (no open DB rec)",
                        )
                    await self.portfolio.record_pnl(realized_pnl)
                    logger.warning(
                        "Closed %s %s @ %.2f (no DB rec; PnL %.2f)",
                        pos.side, pos.symbol, filled_price, realized_pnl,
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
                    if rec.side == "long":
                        rec_pnl = (filled_price - rec_entry) * rec_size - rec_fee
                    else:
                        rec_pnl = (rec_entry - filled_price) * rec_size - rec_fee
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
            except Exception:
                logger.exception("Failed to close position %s", pos.symbol)
                failed += 1

        if failed:
            logger.error(
                "Flat-all completed with %d failures — manual intervention may be required",
                failed,
            )

    async def _update_position_pnl(self) -> None:
        """Update unrealized P&L for all open positions in the DB."""
        if not self.repo:
            return
        open_positions = await self.repo.get_open_positions()
        for pos in open_positions:
            current_price = await self.exchange.get_current_price(pos.symbol)
            if current_price <= 0:
                continue
            if pos.side == "long":
                pnl = (current_price - pos.entry_price) * pos.size
            else:
                pnl = (pos.entry_price - current_price) * pos.size
            await self.repo.update_position_pnl(pos.id, pnl)

    async def _run_strategy(self, strategy: Strategy) -> None:
        # Fetch candles
        candles = await fetch_candles(strategy.symbol, strategy.timeframe)
        if candles.empty:
            logger.warning("No candle data for %s %s", strategy.symbol, strategy.timeframe)
            return

        # Update exchange price (use latest/forming candle for real-time pricing)
        latest_price = candles["close"].iloc[-1]
        logger.info("[%s] %s %s — %d candles, price: $%.2f", strategy.name, strategy.symbol, strategy.timeframe, len(candles), latest_price)
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

            await self._execute_signal(signal, latest_price, leverage=strategy.leverage)

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

    async def _execute_signal(self, signal: Signal, current_price: float, leverage: int = 1) -> bool:
        """Execute a signal end-to-end. Returns True on full success
        (order filled + DB written + parity OK), False on any abort
        (risk-blocked, kill-switch, order rejected, close-size unresolved,
        cap-breached). Callers performing flip-close-then-open MUST check
        the close return value — opening a new opposite position when the
        close failed leaves the DB and exchange permanently divergent.
        """
        # Audit H3: kill-switch + daily-loss cap apply ONLY to OPEN signals.
        # Blocking CLOSE on kill-switch flip would freeze a position-open
        # bot (SL/TP exits wouldn't fire); blocking CLOSE on daily-loss cap
        # would prevent the loss from being realized and capped.
        is_open = signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT)
        if not await self.portfolio.check_risk_limits(is_open=is_open):
            logger.warning(
                "[%s] Risk limit breached — skipping execution of %s %s",
                signal.strategy_name,
                signal.action.value,
                signal.symbol,
            )
            if self.event_bus:
                await self.event_bus.publish(
                    ErrorOccurred(
                        strategy=signal.strategy_name,
                        message=f"Risk limit breached — execution of {signal.action.value} {signal.symbol} blocked",
                    )
                )
            return False

        # Idempotency / flip detection: don't open a same-side duplicate;
        # if we already hold the opposite side, synthesize a CLOSE first
        # so the exchange position fully reverses (instead of HL netting
        # the new open against the existing position and leaving a partial).
        if signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT):
            if self.repo:
                existing = await self.repo.get_open_position(
                    signal.strategy_name, signal.symbol
                )
                if existing:
                    wanted_side = "long" if signal.action == SignalAction.OPEN_LONG else "short"
                    if existing.side == wanted_side:
                        logger.info(
                            "[%s] Skipping %s %s — already have open %s position",
                            signal.strategy_name,
                            signal.action.value,
                            signal.symbol,
                            existing.side,
                        )
                        return False
                    # Opposite side → flip. Close the existing position first
                    # via a synthesized CLOSE signal that goes through the
                    # standard close path (DB-driven size, trade record, etc.).
                    close_action = (
                        SignalAction.CLOSE_SHORT if existing.side == "short"
                        else SignalAction.CLOSE_LONG
                    )
                    logger.info(
                        "[%s] Flip detected — closing %s %s before opening %s "
                        "(reason: %s)",
                        signal.strategy_name,
                        existing.side, signal.symbol, wanted_side,
                        signal.reason[:80],
                    )
                    flip_close = Signal(
                        action=close_action,
                        symbol=signal.symbol,
                        strategy_name=signal.strategy_name,
                        reason=f"Auto-close before flip ({signal.reason[:60]})",
                    )
                    flip_ok = await self._execute_signal(
                        flip_close, current_price, leverage,
                    )
                    if not flip_ok:
                        # ABORT: opening a new opposite-direction position
                        # while the close failed leaves DB+exchange divergent
                        # (audit H1, 2026-05-09). The reconcile loop will
                        # eventually catch the leftover, but we mustn't
                        # actively make it worse here.
                        logger.warning(
                            "[%s] Flip-close FAILED for %s %s — aborting "
                            "follow-up %s to prevent double-side divergence",
                            signal.strategy_name,
                            existing.side, signal.symbol,
                            signal.action.value,
                        )
                        if self.event_bus:
                            try:
                                await self.event_bus.publish(
                                    ErrorOccurred(
                                        strategy=signal.strategy_name,
                                        message=(
                                            f"Flip-close failed on {signal.symbol} "
                                            f"({existing.side}→{wanted_side}); "
                                            f"open aborted to keep DB and "
                                            f"exchange consistent."
                                        ),
                                    )
                                )
                            except Exception:
                                logger.exception("flip-abort: event publish failed")
                        return False

            # Cross-strategy check: block if another strategy already holds
            # this coin and allow_multi_coin is disabled.
            if self.control and self.repo:
                allow_multi = await self.control.get_allow_multi_coin()
                if not allow_multi:
                    blocking = await self.repo.get_open_position_any(signal.symbol)
                    if blocking and blocking.strategy_name != signal.strategy_name:
                        logger.info(
                            "[%s] Skipping %s %s — %s already holds %s on %s (allow_multi_coin=False)",
                            signal.strategy_name,
                            signal.action.value,
                            signal.symbol,
                            blocking.strategy_name,
                            blocking.side,
                            signal.symbol,
                        )
                        return False

            # Total exposure cap: block if opening this would exceed the
            # configured margin sum across all open strategy positions.
            # Audit C1 (2026-05-10): the previous implementation discarded
            # the correctly-computed `current_margin` and approximated each
            # open row as contributing `MAX_POSITION_SIZE_USD` of margin,
            # which made the cap a position-COUNT cap, not a dollar cap.
            # Strategies that emit `Signal(size=…)` (e.g. vvv_hedge) and
            # leveraged strategies were both undercounted.
            if (
                self.repo
                and settings.max_total_exposure_usd > 0
            ):
                open_pos = await self.repo.get_open_positions()
                current_margin = sum(
                    float(p.size) * float(p.entry_price) / max(getattr(p, "leverage", 1) or 1, 1)
                    for p in open_pos
                )
                # Margin for the *new* position being opened. Strategies
                # that override `signal.size` (vvv_hedge sends 400 VVV) get
                # their actual notional counted; others fall back to the
                # standard sized position. Both are divided by leverage.
                lev = max(int(leverage or 1), 1)
                if signal.size:
                    new_notional = float(signal.size) * float(current_price)
                else:
                    new_notional = float(settings.max_position_size_usd) * lev
                new_position_margin = new_notional / lev
                projected_margin = current_margin + new_position_margin
                if projected_margin > settings.max_total_exposure_usd:
                    logger.warning(
                        "[%s] Skipping %s %s — total exposure cap "
                        "would be exceeded: $%.0f current + $%.0f new "
                        "= $%.0f > $%.0f cap",
                        signal.strategy_name,
                        signal.action.value,
                        signal.symbol,
                        current_margin,
                        new_position_margin,
                        projected_margin,
                        settings.max_total_exposure_usd,
                    )
                    return False

        # Audit H1: re-push per-coin leverage to HL before any OPEN if a
        # runtime override has bumped the strategy's `s.leverage` since
        # we last pushed. Per-coin leverage on HL is a single value, so
        # the target is `max(s.leverage)` across strategies trading this
        # coin — matching the startup logic in main.py. No-op when
        # already in sync. CLOSE signals don't need this (leverage
        # affects margin, which only applies to opens).
        if signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT):
            await self._ensure_leverage_pushed(signal.symbol)

        # `is not None` rather than truthy: a strategy emitting size=0 is
        # a bug we should surface, not silently fall back to the default
        # calc. Downstream gates (rounded-to-zero in HL place_order) will
        # reject it cleanly.
        size = (
            signal.size if signal.size is not None
            else self._calculate_size(current_price, leverage)
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
        # signals do. Use `is not None` (not truthy) so a `Signal(size=0)`
        # — which would be a strategy bug, not a legitimate "use default"
        # — flows through here for the cap check rather than silently
        # falling back to `_calculate_size`.
        if (
            signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT)
            and signal.size is not None
        ):
            lev = max(int(leverage or 1), 1)
            max_margin = (
                settings.signal_size_max_multiplier
                * settings.max_position_size_usd
            )
            requested_notional = float(size) * float(current_price)
            requested_margin = requested_notional / lev
            if requested_margin > max_margin:
                logger.warning(
                    "[%s] Skipping %s %s — Signal(size=%s) margin "
                    "$%.0f (notional $%.0f / %dx) exceeds the %.0fx safety "
                    "ceiling $%.0f (SIGNAL_SIZE_MAX_MULTIPLIER × "
                    "MAX_POSITION_SIZE_USD)",
                    signal.strategy_name,
                    signal.action.value,
                    signal.symbol,
                    signal.size,
                    requested_margin,
                    requested_notional,
                    lev,
                    settings.signal_size_max_multiplier,
                    max_margin,
                )
                if self.event_bus:
                    try:
                        await self.event_bus.publish(
                            ErrorOccurred(
                                strategy=signal.strategy_name,
                                message=(
                                    f"Refused {signal.action.value} {signal.symbol}: "
                                    f"signal.size margin ${requested_margin:,.0f} "
                                    f"exceeds {settings.signal_size_max_multiplier}× cap "
                                    f"${max_margin:,.0f}"
                                ),
                            )
                        )
                    except Exception:
                        logger.exception("H8 cap: event publish failed")
                return False

        if signal.action == SignalAction.OPEN_LONG:
            order = await self.exchange.place_order(
                signal.symbol, "buy", size, OrderType.MARKET
            )
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
        elif signal.action == SignalAction.OPEN_SHORT:
            order = await self.exchange.place_order(
                signal.symbol, "sell", size, OrderType.MARKET
            )
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
            return False

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

        # Calculate realized P&L for closes
        realized_pnl: float | None = None
        if signal.action in (SignalAction.CLOSE_LONG, SignalAction.CLOSE_SHORT):
            if self.repo:
                open_pos = await self.repo.get_open_position(
                    signal.strategy_name, signal.symbol
                )
                if open_pos:
                    if open_pos.side == "long":
                        realized_pnl = (filled_price - open_pos.entry_price) * size - fee
                    else:
                        realized_pnl = (open_pos.entry_price - filled_price) * size - fee

        # Record to DB — atomic Trade + PositionRecord write. Audit M8:
        # pre-fix the trade and position writes were two separate sessions,
        # so a SIGTERM / crash between them left a Trade row with no
        # matching PositionRecord (or vice versa for close), which the
        # next reconcile loop would treat as a divergence and force-close.
        if self.repo:
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
                    pnl=realized_pnl or 0,
                    reason=signal.reason,
                )
                await self.portfolio.record_pnl(realized_pnl or 0)

        # Snapshot post-signal strategy state to Redis. Covers the
        # cooldown-after-close gap (audit M6 / PR #19 review): position
        # table state_json only persists during in-position windows;
        # this Redis snapshot persists post-close cooldown so a restart
        # inside the cooldown window can't bypass the re-entry block.
        if self.control:
            strat = next(
                (s for s in self.strategies if s.name == signal.strategy_name),
                None,
            )
            if strat is not None:
                try:
                    snap = strat.export_state()
                    await self.control.save_strategy_state(strat.name, snap)
                except Exception:
                    logger.exception(
                        "[%s] save_strategy_state failed (non-fatal)",
                        signal.strategy_name,
                    )

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
        except Exception:
            logger.exception(
                "parity: exchange.get_positions() failed for %s", symbol,
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
                track_user_address=settings.vault_tracking_address,
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
        """
        if self.repo:
            db_pos = await self.repo.get_open_position(strategy_name, symbol)
            if db_pos is None:
                logger.warning(
                    "[%s] CLOSE_%s ignored for %s — no open DB position",
                    strategy_name, expected_side.upper(), symbol,
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
            except Exception:
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
        ex_pos = await self.exchange.get_position(symbol)
        if not (ex_pos and ex_pos.side == expected_side):
            logger.warning(
                "[%s] CLOSE_%s ignored for %s — no matching exchange position",
                strategy_name, expected_side.upper(), symbol,
            )
            return None
        return ex_pos.size

    async def _ensure_leverage_pushed(self, symbol: str) -> None:
        """Push per-coin leverage to the exchange if it's drifted from
        what we last pushed (audit H1).

        The target is the max `s.leverage` across all strategies trading
        ``symbol`` — matching the startup-time push in `main.py`. Only
        actually calls the exchange when the target differs from
        `_pushed_leverage[symbol]`, so the cost is one dict lookup per
        OPEN tick in the steady state.

        Failures (network blip, exchange rejection) are logged but NOT
        raised — the open continues with whatever leverage the exchange
        currently has. The size calc downstream still divides by
        `signal.leverage`, so the worst case is a margin/notional mismatch
        for one trade until the next push succeeds, instead of the
        liquidation path the audit calls out.
        """
        target = max(
            (s.leverage for s in self.strategies if s.symbol == symbol),
            default=1,
        )
        target = max(int(target), 1)
        previous = self._pushed_leverage.get(symbol)
        if previous == target:
            return
        try:
            ok = await self.exchange.update_leverage(
                symbol, target, is_cross=True,
            )
        except Exception:
            logger.exception(
                "Leverage push failed for %s (target %dx) — "
                "open will proceed with exchange's current leverage",
                symbol, target,
            )
            return
        if ok:
            self._pushed_leverage[symbol] = target
            logger.info(
                "Re-pushed %s leverage=%dx (was %s)",
                symbol, target,
                f"{previous}x" if previous is not None else "unset",
            )
        else:
            logger.warning(
                "Leverage push for %s rejected by exchange (target %dx) — "
                "open will proceed with exchange's current leverage",
                symbol, target,
            )

    def _calculate_size(self, price: float, leverage: int = 1) -> float:
        """Calculate position size in base units.

        Notional = MAX_POSITION_SIZE_USD * leverage. Margin used = MAX_POSITION_SIZE_USD.
        So a $200 max position at 5x means $1000 notional exposure with $200 of margin.
        """
        if price <= 0:
            return 0.0
        notional = settings.max_position_size_usd * min(max(1, int(leverage)), 50)
        return round(notional / price, 6)
