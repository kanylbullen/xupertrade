"""Portfolio tracking and risk management."""

import logging
from datetime import datetime, timezone

from hypertrade.config import settings
from hypertrade.engine.control import BotControl
from hypertrade.events.bus import EventBus
from hypertrade.events.types import ErrorOccurred
from hypertrade.exchange.base import Exchange

logger = logging.getLogger(__name__)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class PortfolioManager:
    """Tracks today's realized PnL and enforces daily-loss + kill-switch caps.

    `_daily_pnl` is mirrored to Redis via `BotControl` so the
    `MAX_DAILY_LOSS_USD` cap survives container restarts (audit C2). Without
    Redis the counter degrades to in-memory only — fine for tests and paper,
    NOT acceptable for mainnet (deploy guards against this elsewhere).

    Both Redis-backed guards fail CLOSED (`analysis-2026-09-15.md` § 4):
    a kill-switch flag that cannot be read blocks opens, and a daily-PnL
    total that cannot be written is retried and reported rather than
    silently dropped.
    """

    def __init__(
        self,
        exchange: Exchange,
        control: BotControl | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.exchange = exchange
        self.control = control
        self.event_bus = event_bus
        self._daily_pnl: float = 0.0
        self._date_str: str = _today_str()
        self._loaded: bool = False
        # The running total holds PnL Redis does not have yet. Retried on
        # the next record_pnl and the next risk check until a write lands.
        self._persist_pending: bool = False
        # Outage latches: one WARNING / one delivered ErrorOccurred per
        # outage, not one per call. Cleared by the next successful read /
        # write.
        self._kill_switch_unreadable: bool = False
        self._persist_failure_logged: bool = False
        self._persist_alerted: bool = False
        # NU-2, set by the runner: why this bot's opens are blocked — its
        # reconcile hold (in memory, so an unwritten one holds too) or a
        # wait for a reconcile pass. A kill switch scoped to this bot; the
        # kill switch key is shared by every tenant of the mode.
        self.opens_held: str | None = None

    async def _ensure_loaded(self) -> None:
        """Load today's PnL from Redis once per process start and once
        per UTC day. Cheap to call every tick.

        A failed load leaves `_loaded` False, so the next call retries.
        Until it succeeds, `_daily_pnl` holds only the PnL recorded since
        the load failed; a successful load adds it to the stored total
        instead of replacing it. Nothing is written back before a load
        succeeds — a write then would overwrite the day's stored loss
        with just the trades this process happened to see.
        """
        today = _today_str()
        if today != self._date_str:
            # UTC rollover: yesterday's counter no longer gates anything.
            self._date_str = today
            self._daily_pnl = 0.0
            self._loaded = False
            self._persist_pending = False
        if self._loaded:
            return
        if self.control is None:
            self._loaded = True
            return
        try:
            stored = await self.control.get_daily_pnl(today)
        except Exception:
            logger.exception(
                "Failed to load daily_pnl from Redis — using in-memory %.2f "
                "until a load succeeds",
                self._daily_pnl,
            )
            return
        unloaded = self._daily_pnl
        self._daily_pnl = stored + unloaded
        self._loaded = True
        if stored != 0.0:
            logger.info(
                "Restored daily_pnl from Redis: $%.2f for %s",
                stored, today,
            )
        if unloaded != 0.0:
            logger.warning(
                "daily_pnl: adding $%.2f recorded while Redis was unreadable "
                "to the stored $%.2f (total $%.2f)",
                unloaded, stored, self._daily_pnl,
            )
            self._persist_pending = True

    async def _persist(self) -> None:
        """Write today's running total to Redis. Never raises.

        A failure keeps `_persist_pending` set so the next record or risk
        check retries it, and publishes ONE ErrorOccurred per outage: a
        Redis blip followed by a restart would otherwise reset the
        daily-loss counter with nothing but a log line to show for it.
        """
        if self.control is None:
            self._persist_pending = False
            return
        if not self._loaded:
            self._persist_pending = True
            await self._report_persist_failure(
                "today's stored total could not be read, so the running "
                "total cannot be written without overwriting it"
            )
            return
        try:
            await self.control.set_daily_pnl(self._date_str, self._daily_pnl)
        except Exception as e:
            self._persist_pending = True
            logger.exception(
                "Failed to persist daily_pnl to Redis — $%.2f still tracked "
                "in-memory, retried on the next record or risk check",
                self._daily_pnl,
            )
            await self._report_persist_failure(f"{type(e).__name__}: {e}")
            return
        self._persist_pending = False
        if self._persist_failure_logged or self._persist_alerted:
            self._persist_failure_logged = False
            self._persist_alerted = False
            logger.warning(
                "daily_pnl persisted again ($%.2f for %s) — Redis write recovered",
                self._daily_pnl, self._date_str,
            )

    async def _report_persist_failure(self, reason: str) -> None:
        """Log once per outage; publish until the alert is delivered.

        The alert travels over the same Redis whose write just failed, so
        in a Redis-wide outage the first publish is lost too. The latch is
        set only once the event bus reports the event as delivered;
        until then every later failure (next record, next risk check)
        tries the publish again.
        """
        if self._persist_alerted:
            return
        message = (
            f"Daily PnL (${self._daily_pnl:,.2f} for {self._date_str}) could "
            f"not be saved to Redis: {reason}. The MAX_DAILY_LOSS_USD counter "
            f"lives only in this process until a write succeeds — a restart "
            f"now would reset it. Retrying on every trade and risk check."
        )
        if not self._persist_failure_logged:
            self._persist_failure_logged = True
            logger.error("%s", message)
        if self.event_bus is None:
            return
        try:
            delivered = await self.event_bus.publish(
                ErrorOccurred(strategy="daily-pnl", message=message)
            )
        except Exception:
            logger.exception("daily-pnl: event publish failed")
            return
        # `False` is the bus saying it could not deliver; anything else
        # (True, or None from a bus that does not report) counts as sent.
        if delivered is not False:
            self._persist_alerted = True

    async def _kill_switch_active(self) -> bool:
        """The effective kill switch for OPENS.

        Audit H7: the Redis-backed runtime override wins over the env
        default whenever it is set (the operator can activate or
        deactivate at runtime); unset → `settings.kill_switch`. A flag we
        cannot read counts as ACTIVE — the old fallback to the env
        default (normally False) meant a Redis blip switched off a kill
        switch the operator had turned on. Logged once per outage.
        """
        if self.control is None:
            return settings.kill_switch
        try:
            redis_state = await self.control.is_kill_switch_active()
        except Exception:
            if not self._kill_switch_unreadable:
                self._kill_switch_unreadable = True
                logger.warning(
                    "Kill-switch flag unreadable in Redis — treating the kill "
                    "switch as ACTIVE: new opens blocked, closes unaffected, "
                    "until the flag can be read again",
                    exc_info=True,
                )
            return True
        if self._kill_switch_unreadable:
            self._kill_switch_unreadable = False
            logger.warning("Kill-switch flag readable again — opens follow it")
        return settings.kill_switch if redis_state is None else redis_state

    async def check_risk_limits(self, is_open: bool = True) -> bool:
        """Returns True if trading is allowed, False if limits hit.

        ``is_open=True`` (default) checks the kill-switch and the daily-loss
        cap — both apply to NEW positions. ``is_open=False`` skips both:
        CLOSE signals must always be allowed through, otherwise flipping
        the kill-switch on a position-already-open bot freezes the
        position and SL/TP exits don't fire (audit H3, 2026-05-10).
        Closing a position can only REDUCE risk, never add it.
        """
        await self._ensure_loaded()
        if self._persist_pending:
            await self._persist()

        if not is_open:
            return True

        # Check kill switch (OPEN only — see docstring).
        if await self._kill_switch_active():
            if not self._kill_switch_unreadable:
                logger.warning("Kill switch active — new opens disabled")
            return False

        if self.opens_held:
            logger.warning("New opens disabled — %s", self.opens_held)
            return False

        # Check daily loss limit (OPEN only — closing reduces risk)
        if self._daily_pnl < -settings.max_daily_loss_usd:
            logger.warning(
                "Daily loss limit hit: $%.2f (limit: $%.2f)",
                self._daily_pnl,
                settings.max_daily_loss_usd,
            )
            return False

        return True

    async def record_pnl(self, pnl: float) -> None:
        await self._ensure_loaded()
        self._daily_pnl += pnl
        await self._persist()
