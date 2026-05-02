"""SQLAlchemy models for trade history and strategy state."""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Boolean,
    Text,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, nullable=False)
    strategy_name = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)  # buy/sell
    size = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    pnl = Column(Float, nullable=True)
    reason = Column(Text, default="")
    is_paper = Column(Boolean, default=True, index=True)  # legacy: True for paper, False for testnet/mainnet
    mode = Column(String(16), default="paper", index=True)  # paper | testnet | mainnet
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class PositionRecord(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_name = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(8), nullable=False)  # long/short
    size = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    is_open = Column(Boolean, default=True, index=True)
    is_paper = Column(Boolean, default=True, index=True)
    mode = Column(String(16), default="paper", index=True)
    # Strategy-internal state at signal time (JSON: e.g. {"sl": 1.23, "tp": 4.56,
    # "entry": 2.0, "trail_extreme": null}). On restart, restore_state reads
    # these exact values to eliminate SL drift across restarts. Strategies
    # that don't persist state leave this None — fallback is recompute.
    state_json = Column(Text, nullable=True)
    opened_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    closed_at = Column(DateTime(timezone=True), nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    total_equity = Column(Float, nullable=False)
    available_balance = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, default=0.0)
    is_paper = Column(Boolean, default=True, index=True)
    mode = Column(String(16), default="paper", index=True)
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class FundingPayment(Base):
    """A single funding payment received or paid by the account.

    Pulled periodically from HL's user_funding_history. usdc is signed:
    positive = received, negative = paid. Attribution to a strategy is
    best-effort: at insertion time, we look up the open position on this
    coin and tag with that strategy_name. NULL if no DB position covered
    the payment time (rare — typically only for orphan positions).
    """

    __tablename__ = "funding_payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # HL's funding event time (epoch ms → DateTime)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    # HL hash for idempotency. user_funding_history can return overlapping
    # ranges so we dedupe on this.
    hash = Column(String(80), unique=True, nullable=False)
    coin = Column(String(16), nullable=False, index=True)
    usdc = Column(Float, nullable=False)
    szi = Column(Float, nullable=True)  # signed position size at funding time
    funding_rate = Column(Float, nullable=True)
    strategy_name = Column(String(64), nullable=True, index=True)
    is_paper = Column(Boolean, default=False, index=True)
    mode = Column(String(16), default="testnet", index=True)


class BacktestRun(Base):
    """Persisted backtest result. One row per CLI invocation per strategy.
    Lets us compare runs over time, see how parameter changes affect APR,
    and surface results in the dashboard. Trades and equity-curve are
    NOT stored (would explode the table); just the summary metrics."""

    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_name = Column(String(64), nullable=False, index=True)
    symbol = Column(String(16), nullable=False)
    timeframe = Column(String(8), nullable=False)
    leverage = Column(Integer, nullable=False, default=1)
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    days = Column(Float, nullable=False)
    initial_equity = Column(Float, nullable=False)
    final_equity = Column(Float, nullable=False)
    total_return_pct = Column(Float, nullable=False)
    apr = Column(Float, nullable=False)
    sharpe = Column(Float, nullable=False)
    max_drawdown_pct = Column(Float, nullable=False)
    num_trades = Column(Integer, nullable=False)
    num_round_trips = Column(Integer, nullable=False)
    wins = Column(Integer, nullable=False)
    losses = Column(Integer, nullable=False)
    win_rate = Column(Float, nullable=False)
    fees_paid = Column(Float, nullable=False)
    position_size_usd = Column(Float, nullable=False)
    fee_rate = Column(Float, nullable=False)
    slippage_bps = Column(Float, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)
    symbol = Column(String(16), nullable=False)
    timeframe = Column(String(8), nullable=False)
    enabled = Column(Boolean, default=True)
    params_json = Column(Text, default="{}")
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
