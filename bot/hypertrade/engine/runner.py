"""Strategy engine runner — the core loop."""

import logging
import time

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
    LogEntry,
    SignalGenerated,
    TickCompleted,
    TradeExecuted,
)
from hypertrade.exchange.base import Exchange, OrderType
from hypertrade.strategies.base import Strategy

logger = logging.getLogger(__name__)

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
        self.portfolio = PortfolioManager(exchange)
        self._last_reconcile = 0.0  # epoch seconds
        self._last_funding_poll = 0.0
        self._last_hodl_check = 0.0
        self._last_hodl_zones: dict[str, str] = {}  # signal_name -> last verdict
        self._last_vault_poll = 0.0
        self._vault_poller = None  # lazy-init on first use
        self._last_rate_check = 0.0
        self._rate_alarm_paused: set[str] = set()  # strategies auto-paused this run

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

        self._kick_caddy_tls_restore()

    def _kick_caddy_tls_restore(self) -> None:
        """Fire-and-forget re-push of the persisted Caddy TLS config.

        Caddy boots from the Caddyfile mount with `tls internal` (self-signed),
        because the LE config is held in Redis on the bot side and pushed
        dynamically. Without this, every redeploy reverts the dashboard to
        the self-signed bootstrap cert until somebody clicks Save on
        Options → TLS.

        Run as a background task so a slow / unreachable Caddy can't
        delay engine startup. Gated to the testnet bot so paper / mainnet
        don't race to push the same config three times.
        """
        # Only the testnet bot owns operational tasks (Telegram, vault
        # scanner, TLS re-apply). Other modes share the same Postgres/Redis
        # but stay out of side-effecting orchestration.
        if settings.exchange_mode != "testnet":
            return
        if self.control is None:
            return

        async def _restore() -> None:
            from hypertrade.notify import caddy_admin
            ok, msg = await caddy_admin.push_persisted_config(self.control)
            if ok:
                logger.info("TLS restore: re-pushed Caddy config (%s)", msg)
            else:
                # Includes "missing fields: ..." when state is incomplete
                # and "HTTP 5xx: ..." or "ConnectionError: ..." otherwise.
                logger.warning("TLS restore: skipped — %s", msg)

        # asyncio.create_task: fires immediately, doesn't block startup.
        # The runner's tick loop keeps the event loop alive long enough
        # for the task to complete.
        import asyncio
        try:
            asyncio.create_task(_restore())
        except RuntimeError:
            # No running event loop (shouldn't happen — startup is async),
            # but keep startup robust against weird call paths.
            logger.exception("TLS restore: could not schedule background task")

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
                except Exception:
                    logger.exception("Error running strategy %s", strategy.name)
                    if self.event_bus:
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
                        reason="Flat-all from dashboard",
                    )
                    # Find the open position record (any strategy) and close it
                    open_rec = await self.repo.get_open_position_any(pos.symbol)
                    if open_rec:
                        await self.repo.close_position(
                            open_rec.strategy_name,
                            pos.symbol,
                            filled_price,
                            realized_pnl,
                        )
                self.portfolio.record_pnl(realized_pnl)
                logger.warning(
                    "Closed %s %s @ %.2f (PnL %.2f)",
                    pos.side,
                    pos.symbol,
                    filled_price,
                    realized_pnl,
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
        if not await self.portfolio.check_risk_limits():
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

        if settings.kill_switch:
            logger.warning("Kill switch is ON — skipping execution")
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
            if (
                self.repo
                and settings.max_total_exposure_usd > 0
            ):
                open_pos = await self.repo.get_open_positions()
                current_margin = sum(
                    float(p.size) * float(p.entry_price) / max(getattr(p, "leverage", 1) or 1, 1)
                    for p in open_pos
                )
                # Approximation: each open strategy position contributes
                # MAX_POSITION_SIZE_USD of margin (the same cap used at open).
                approx_existing_margin = len(open_pos) * settings.max_position_size_usd
                if approx_existing_margin + settings.max_position_size_usd > settings.max_total_exposure_usd:
                    logger.warning(
                        "[%s] Skipping %s %s — total exposure cap "
                        "reached: %d open positions × $%.0f >= $%.0f",
                        signal.strategy_name,
                        signal.action.value,
                        signal.symbol,
                        len(open_pos),
                        settings.max_position_size_usd,
                        settings.max_total_exposure_usd,
                    )
                    return False

        size = signal.size or self._calculate_size(current_price, leverage)

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

        # Record to DB
        if self.repo:
            await self.repo.record_trade(
                order_id=order.id,
                strategy_name=signal.strategy_name,
                symbol=signal.symbol,
                side=order.side,
                size=size,
                price=filled_price,
                fee=fee,
                pnl=realized_pnl,
                reason=signal.reason,
            )

            if signal.action in (SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT):
                import json as _json
                side = "long" if signal.action == SignalAction.OPEN_LONG else "short"
                # Snapshot strategy state at signal time so restart can
                # restore the exact SL/TP/etc. without recomputation drift.
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
                await self.repo.open_position(
                    signal.strategy_name,
                    signal.symbol,
                    side,
                    size,
                    filled_price,
                    state_json=state_json,
                )
            elif signal.action in (SignalAction.CLOSE_LONG, SignalAction.CLOSE_SHORT):
                await self.repo.close_position(
                    signal.strategy_name,
                    signal.symbol,
                    filled_price,
                    realized_pnl or 0,
                )
                self.portfolio.record_pnl(realized_pnl or 0)

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
        |db_net_size - exchange_net_size| > tolerance. Per-coin tolerance
        is derived dynamically from `exchange.get_size_precision(symbol)`
        — set to 10× HL's szDecimals minimum step (audit M4):
          BTC szDecimals=5 → tolerance 1e-4
          ETH szDecimals=4 → tolerance 1e-3
          SOL szDecimals=2 → tolerance 1e-1
        Default fallback (unknown coin) is szDecimals=4 → 1e-3.

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
        # Tolerance = 10× the exchange's minimum step. `Exchange` base
        # defines `get_size_precision` with default 4dp, so the call
        # is safe for any concrete exchange. (Audit M4.)
        sz_decimals = self.exchange.get_size_precision(symbol)
        tolerance = 10 * (10 ** -sz_decimals)
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

            # Verdict changed → emit event so Telegram forwards it
            if self.event_bus:
                try:
                    await self.event_bus.publish(
                        ErrorOccurred(
                            strategy=f"hodl/{sig.name}",
                            message=(
                                f"HODL verdict changed for {sig.asset}: "
                                f"{prev} → {state.verdict}"
                            ),
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

    def _calculate_size(self, price: float, leverage: int = 1) -> float:
        """Calculate position size in base units.

        Notional = MAX_POSITION_SIZE_USD * leverage. Margin used = MAX_POSITION_SIZE_USD.
        So a $200 max position at 5x means $1000 notional exposure with $200 of margin.
        """
        if price <= 0:
            return 0.0
        notional = settings.max_position_size_usd * min(max(1, int(leverage)), 50)
        return round(notional / price, 6)
