from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(str, Enum):
    OPEN_LONG = "open_long"
    CLOSE_LONG = "close_long"
    OPEN_SHORT = "open_short"
    CLOSE_SHORT = "close_short"
    HOLD = "hold"


@dataclass
class Signal:
    action: SignalAction
    symbol: str
    size: float | None = None  # None = use default from risk mgmt
    price: float | None = None  # None = market order
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy_name: str = ""
    reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
