"""Event types for the event bus."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json


@dataclass
class Event:
    type: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    mode: str = ""  # paper / testnet / mainnet — populated by EventBus.publish

    def to_json(self) -> str:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return json.dumps(d)

    @classmethod
    def from_json(cls, data: str) -> "Event":
        return cls(**json.loads(data))


@dataclass
class SignalGenerated(Event):
    type: str = "signal.generated"
    strategy: str = ""
    symbol: str = ""
    action: str = ""
    reason: str = ""


@dataclass
class TradeExecuted(Event):
    type: str = "trade.executed"
    strategy: str = ""
    symbol: str = ""
    side: str = ""
    size: float = 0.0
    price: float = 0.0
    order_id: str = ""
    reason: str = ""


@dataclass
class PositionOpened(Event):
    type: str = "position.opened"
    strategy: str = ""
    symbol: str = ""
    side: str = ""
    size: float = 0.0
    entry_price: float = 0.0


@dataclass
class PositionClosed(Event):
    type: str = "position.closed"
    strategy: str = ""
    symbol: str = ""
    pnl: float = 0.0
    exit_price: float = 0.0


@dataclass
class ErrorOccurred(Event):
    type: str = "error"
    strategy: str = ""
    message: str = ""


@dataclass
class BotHeartbeat(Event):
    type: str = "bot.heartbeat"
    mode: str = ""
    strategies: str = ""  # comma-separated
    equity: float = 0.0
    positions: int = 0
    uptime_seconds: int = 0


@dataclass
class TickCompleted(Event):
    type: str = "tick.completed"
    strategy: str = ""
    symbol: str = ""
    timeframe: str = ""
    price: float = 0.0
    signal: str = ""  # "none" or signal action
    reason: str = ""


@dataclass
class LogEntry(Event):
    type: str = "log"
    level: str = "info"
    message: str = ""


@dataclass
class VaultQualified(Event):
    """A vault entered the qualified set since the last poll."""

    type: str = "vault.qualified"
    address: str = ""
    name: str = ""
    apr: float = 0.0
    aum_usd: float = 0.0
    sharpe_180d: float = 0.0
    leader_equity_pct: float = 0.0


@dataclass
class VaultDisqualified(Event):
    """A previously qualified vault failed at least one filter."""

    type: str = "vault.disqualified"
    address: str = ""
    name: str = ""
    failed_filters: str = ""  # comma-separated filter names
