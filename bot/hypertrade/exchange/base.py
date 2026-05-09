from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


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
    async def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None:
        ...

    @abstractmethod
    async def get_balance(self) -> Balance:
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

    def get_size_precision(self, symbol: str) -> int:
        """Return szDecimals (size-precision) for a coin. Drives the
        parity-check tolerance — anything within `10**(-szDecimals)` is
        within the exchange's minimum step and shouldn't trigger an alert.
        Default 4 dp covers most coins (BTC=5, ETH=4, SOL=2 on HL).
        Override in concrete exchanges that know per-coin precision.
        """
        return 4
