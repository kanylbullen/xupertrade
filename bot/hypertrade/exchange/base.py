from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ExchangeReadError(Exception):
    """A read from the exchange could not be completed.

    The point of this type is to keep "the exchange says you are flat"
    distinguishable from "we could not ask the exchange". Returning an
    empty list for the second case is what let a single HyperLiquid 502
    flatten the whole book: reconcile saw zero exchange positions,
    orphan-closed every open DB row at PnL 0, and five minutes later
    market-closed the still-real exchange positions
    (`bot/reports/analysis-2026-09-15.md` § 2).

    Every read that can fail raises this instead. Callers must decide
    explicitly what to do without an answer — skip the action, return
    503, refuse to acknowledge — never silently treat it as "flat".
    """


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    id: str
    symbol: str
    side: str  # "buy" or "sell"
    size: float
    order_type: OrderType
    price: float | None = None
    filled_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Position:
    symbol: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0
    liquidation_price: float | None = None


@dataclass
class Balance:
    total: float
    available: float
    unrealized_pnl: float = 0.0


class Exchange(ABC):
    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel a resting order. True only when the exchange confirmed it.

        `symbol` is required: HyperLiquid identifies an order by coin and
        oid together, so an id alone cannot be cancelled.
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Open positions. Raises ExchangeReadError if the read failed.

        An empty list means "flat", never "could not read".
        """
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        """One coin's position. Raises ExchangeReadError if unreadable."""
        ...

    @abstractmethod
    async def get_balance(self) -> Balance:
        """Account balance. Raises ExchangeReadError if unreadable."""
        ...

    @abstractmethod
    async def get_current_price(self, symbol: str) -> float:
        ...

    async def update_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> bool:
        """Set leverage for a coin. Default impl is a no-op (paper exchange)."""
        return True

    async def get_user_funding_history(
        self, start_time_ms: int, end_time_ms: int | None = None
    ) -> list[dict]:
        """Fetch funding events. Default no-op for paper / non-perpetual."""
        return []

    async def fetch_user_fills(
        self, address: str | None = None, since_ms: int | None = None,
    ) -> list[dict]:
        """Fetch raw exchange fill records. Default no-op for paper."""
        return []

    def get_size_precision(self, symbol: str) -> int:
        """Return szDecimals (size-precision) for a coin. Drives the
        parity-check tolerance — anything within `10**(-szDecimals)` is
        within the exchange's minimum step and shouldn't trigger an alert.
        Default 4 dp covers most coins (BTC=5, ETH=4, SOL=2 on HL).
        Override in concrete exchanges that know per-coin precision.
        """
        return 4
