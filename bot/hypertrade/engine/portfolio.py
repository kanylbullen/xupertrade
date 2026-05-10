"""Portfolio tracking and risk management."""

import logging
from datetime import datetime, timezone

from hypertrade.config import settings
from hypertrade.engine.control import BotControl
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
    """

    def __init__(self, exchange: Exchange, control: BotControl | None = None) -> None:
        self.exchange = exchange
        self.control = control
        self._daily_pnl: float = 0.0
        self._date_str: str = _today_str()
        self._loaded: bool = False

    async def _ensure_loaded(self) -> None:
        """Load today's PnL from Redis once per process start, and reset
        when the UTC date rolls over. Cheap to call every tick.

        Redis errors here are caught and logged — falling back to the
        last in-memory value (or 0.0 on cold start) means a Redis blip
        can't crash the engine tick or the post-trade record path. The
        worst case is a missed restore for one tick; the next call to
        `_ensure_loaded` retries.
        """
        today = _today_str()
        if not self._loaded or today != self._date_str:
            self._date_str = today
            if self.control is not None:
                try:
                    self._daily_pnl = await self.control.get_daily_pnl(today)
                except Exception:
                    logger.exception(
                        "Failed to load daily_pnl from Redis — using in-memory %.2f",
                        self._daily_pnl,
                    )
                    # Don't mark loaded on failure — let the next call retry.
                    return
                if self._daily_pnl != 0.0:
                    logger.info(
                        "Restored daily_pnl from Redis: $%.2f for %s",
                        self._daily_pnl, today,
                    )
            else:
                self._daily_pnl = 0.0
            self._loaded = True

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

        if not is_open:
            return True

        # Check kill switch (OPEN only — see docstring)
        if settings.kill_switch:
            logger.warning("Kill switch active — new opens disabled")
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
        if self.control is not None:
            try:
                await self.control.set_daily_pnl(self._date_str, self._daily_pnl)
            except Exception:
                logger.exception(
                    "Failed to persist daily_pnl to Redis — value still tracked in-memory"
                )
