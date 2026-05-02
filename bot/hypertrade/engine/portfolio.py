"""Portfolio tracking and risk management."""

import logging
from datetime import datetime, timezone

from hypertrade.config import settings
from hypertrade.exchange.base import Exchange

logger = logging.getLogger(__name__)


class PortfolioManager:
    def __init__(self, exchange: Exchange) -> None:
        self.exchange = exchange
        self._daily_pnl: float = 0.0
        self._last_reset: datetime = datetime.now(timezone.utc)

    async def check_risk_limits(self) -> bool:
        """Returns True if trading is allowed, False if limits hit."""
        now = datetime.now(timezone.utc)

        # Reset daily P&L at midnight UTC
        if now.date() != self._last_reset.date():
            self._daily_pnl = 0.0
            self._last_reset = now

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

    def record_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl
