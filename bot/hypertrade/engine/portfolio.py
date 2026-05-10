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
        when the UTC date rolls over. Cheap to call every tick."""
        today = _today_str()
        if not self._loaded or today != self._date_str:
            self._date_str = today
            if self.control is not None:
                self._daily_pnl = await self.control.get_daily_pnl(today)
                if self._daily_pnl != 0.0:
                    logger.info(
                        "Restored daily_pnl from Redis: $%.2f for %s",
                        self._daily_pnl, today,
                    )
            else:
                self._daily_pnl = 0.0
            self._loaded = True

    async def check_risk_limits(self) -> bool:
        """Returns True if trading is allowed, False if limits hit."""
        await self._ensure_loaded()

        # Check kill switch
        if settings.kill_switch:
            logger.warning("Kill switch active — trading disabled")
            return False

        # Check daily loss limit
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
