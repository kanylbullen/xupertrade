"""Backtest engine.

Replays historical candles to a strategy and simulates fills + fees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from hypertrade.backtest.metrics import (
    annualized_return,
    max_drawdown_pct,
    periods_per_year_for_timeframe,
    sharpe_ratio,
)
from hypertrade.engine.signals import SignalAction
from hypertrade.strategies.base import Strategy

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    timestamp: datetime
    side: str  # "buy" or "sell"
    price: float
    size: float
    fee: float
    pnl: float | None = None  # set on the closing fill
    reason: str = ""


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    initial_equity: float
    final_equity: float
    trades: list[BacktestTrade]
    equity_curve: list[tuple[datetime, float]]
    fees_paid: float

    # Caches for derived metrics
    _sharpe: float | None = field(default=None, init=False, repr=False)
    _max_dd: float | None = field(default=None, init=False, repr=False)

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86_400.0

    @property
    def total_return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return (self.final_equity - self.initial_equity) / self.initial_equity

    @property
    def apr(self) -> float:
        return annualized_return(self.initial_equity, self.final_equity, self.days)

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def num_round_trips(self) -> int:
        return sum(1 for t in self.trades if t.pnl is not None)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl is not None and t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl is not None and t.pnl < 0)

    @property
    def win_rate(self) -> float:
        decisive = self.wins + self.losses
        return self.wins / decisive if decisive > 0 else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if self._max_dd is None:
            self._max_dd = max_drawdown_pct([e for _, e in self.equity_curve])
        return self._max_dd

    @property
    def sharpe(self) -> float:
        if self._sharpe is None:
            curve = [e for _, e in self.equity_curve]
            if len(curve) < 2:
                self._sharpe = 0.0
            else:
                returns = [
                    (curve[i] - curve[i - 1]) / curve[i - 1]
                    for i in range(1, len(curve))
                    if curve[i - 1] > 0
                ]
                self._sharpe = sharpe_ratio(
                    returns, periods_per_year_for_timeframe(self.timeframe)
                )
        return self._sharpe

    def format_summary(self) -> str:
        lines = [
            f"Backtest — {self.strategy} on {self.symbol} {self.timeframe}",
            f"  Period:        {self.start:%Y-%m-%d} → {self.end:%Y-%m-%d}  ({self.days:.0f} days)",
            f"  Initial:       ${self.initial_equity:,.2f}",
            f"  Final:         ${self.final_equity:,.2f}",
            f"  Total return:  {self.total_return_pct * 100:+.2f}%",
            f"  APR:           {self.apr * 100:+.2f}%",
            f"  Sharpe:        {self.sharpe:.2f}",
            f"  Max drawdown:  {self.max_drawdown_pct * 100:.2f}%",
            f"  Trades:        {self.num_trades} ({self.num_round_trips} round trips)",
            f"  Win rate:      {self.win_rate * 100:.1f}% ({self.wins}W / {self.losses}L)",
            f"  Fees paid:     ${self.fees_paid:,.2f}",
        ]
        return "\n".join(lines)


async def run_backtest(
    strategy: Strategy,
    candles: pd.DataFrame,
    initial_equity: float = 10_000.0,
    position_size_usd: float = 1_000.0,
    fee_rate: float = 0.00045,
    slippage_bps: float = 5.0,
    warmup_bars: int | None = None,
) -> BacktestResult:
    """Replay candles through a strategy and return a BacktestResult.

    Constraints (intentionally simple — accept the limitations):
    - One position at a time per backtest
    - Fills happen at the bar's close price, with optional slippage_bps
    - Fees applied at fee_rate (taker default)
    - Strategy.leverage is honored via position_size_usd × leverage
    """
    if candles.empty:
        raise ValueError("Empty candle DataFrame")
    if "timestamp" not in candles.columns:
        raise ValueError("candles must have a 'timestamp' column")

    df = candles.sort_values("timestamp").reset_index(drop=True)
    n = len(df)

    # Strategy needs enough warmup before the first signal can be valid.
    # Default to 20% of the data or 250 bars, whichever is smaller.
    if warmup_bars is None:
        warmup_bars = min(250, max(50, n // 5))

    leverage = max(1, getattr(strategy, "leverage", 1) or 1)
    notional = position_size_usd * leverage

    equity = initial_equity
    fees_paid = 0.0
    trades: list[BacktestTrade] = []
    equity_curve: list[tuple[datetime, float]] = []

    open_side: str | None = None  # "long" or "short"
    open_size: float = 0.0
    open_entry: float = 0.0

    slippage_factor = slippage_bps / 10_000.0

    for i in range(warmup_bars, n):
        window = df.iloc[: i + 1]
        bar = df.iloc[i]
        ts = bar["timestamp"]
        close = float(bar["close"])

        # Mark-to-market equity at this bar's close
        marked_equity = equity
        if open_side == "long":
            marked_equity = equity + (close - open_entry) * open_size
        elif open_side == "short":
            marked_equity = equity + (open_entry - close) * open_size
        equity_curve.append((ts, marked_equity))

        signal = await strategy.on_candle(window)
        if signal is None:
            continue

        action = signal.action

        if action == SignalAction.OPEN_LONG and open_side is None:
            fill = close * (1 + slippage_factor)
            size = notional / fill
            fee = fill * size * fee_rate
            equity -= fee
            fees_paid += fee
            open_side = "long"
            open_size = size
            open_entry = fill
            trades.append(BacktestTrade(
                timestamp=ts, side="buy", price=fill, size=size,
                fee=fee, reason=signal.reason,
            ))

        elif action == SignalAction.OPEN_SHORT and open_side is None:
            fill = close * (1 - slippage_factor)
            size = notional / fill
            fee = fill * size * fee_rate
            equity -= fee
            fees_paid += fee
            open_side = "short"
            open_size = size
            open_entry = fill
            trades.append(BacktestTrade(
                timestamp=ts, side="sell", price=fill, size=size,
                fee=fee, reason=signal.reason,
            ))

        elif action == SignalAction.CLOSE_LONG and open_side == "long":
            fill = close * (1 - slippage_factor)
            gross = (fill - open_entry) * open_size
            fee = fill * open_size * fee_rate
            net = gross - fee
            equity += gross - fee
            fees_paid += fee
            trades.append(BacktestTrade(
                timestamp=ts, side="sell", price=fill, size=open_size,
                fee=fee, pnl=net, reason=signal.reason,
            ))
            open_side = None
            open_size = 0.0
            open_entry = 0.0

        elif action == SignalAction.CLOSE_SHORT and open_side == "short":
            fill = close * (1 + slippage_factor)
            gross = (open_entry - fill) * open_size
            fee = fill * open_size * fee_rate
            net = gross - fee
            equity += gross - fee
            fees_paid += fee
            trades.append(BacktestTrade(
                timestamp=ts, side="buy", price=fill, size=open_size,
                fee=fee, pnl=net, reason=signal.reason,
            ))
            open_side = None
            open_size = 0.0
            open_entry = 0.0

        # OPEN_X while a same-side position is open → ignore (live engine does
        # the same). OPEN_X while opposite side is open → live engine flips
        # via synthesized close. For simplicity here we synthesize the same
        # close-then-open inline.
        elif action == SignalAction.OPEN_LONG and open_side == "short":
            # close short
            fill_close = close * (1 + slippage_factor)
            gross = (open_entry - fill_close) * open_size
            fee_close = fill_close * open_size * fee_rate
            equity += gross - fee_close
            fees_paid += fee_close
            trades.append(BacktestTrade(
                timestamp=ts, side="buy", price=fill_close, size=open_size,
                fee=fee_close, pnl=gross - fee_close,
                reason=f"Auto-close before flip ({signal.reason[:60]})",
            ))
            # open long
            fill_open = close * (1 + slippage_factor)
            size = notional / fill_open
            fee_open = fill_open * size * fee_rate
            equity -= fee_open
            fees_paid += fee_open
            open_side = "long"
            open_size = size
            open_entry = fill_open
            trades.append(BacktestTrade(
                timestamp=ts, side="buy", price=fill_open, size=size,
                fee=fee_open, reason=signal.reason,
            ))

        elif action == SignalAction.OPEN_SHORT and open_side == "long":
            # close long
            fill_close = close * (1 - slippage_factor)
            gross = (fill_close - open_entry) * open_size
            fee_close = fill_close * open_size * fee_rate
            equity += gross - fee_close
            fees_paid += fee_close
            trades.append(BacktestTrade(
                timestamp=ts, side="sell", price=fill_close, size=open_size,
                fee=fee_close, pnl=gross - fee_close,
                reason=f"Auto-close before flip ({signal.reason[:60]})",
            ))
            # open short
            fill_open = close * (1 - slippage_factor)
            size = notional / fill_open
            fee_open = fill_open * size * fee_rate
            equity -= fee_open
            fees_paid += fee_open
            open_side = "short"
            open_size = size
            open_entry = fill_open
            trades.append(BacktestTrade(
                timestamp=ts, side="sell", price=fill_open, size=size,
                fee=fee_open, reason=signal.reason,
            ))

    # Mark-to-market close any open position at the last candle close
    final_equity = equity
    if open_side == "long":
        final_equity = equity + (float(df.iloc[-1]["close"]) - open_entry) * open_size
    elif open_side == "short":
        final_equity = equity + (open_entry - float(df.iloc[-1]["close"])) * open_size

    start_ts = df.iloc[warmup_bars]["timestamp"] if n > warmup_bars else df.iloc[0]["timestamp"]
    end_ts = df.iloc[-1]["timestamp"]

    return BacktestResult(
        strategy=strategy.name,
        symbol=strategy.symbol,
        timeframe=strategy.timeframe,
        start=start_ts,
        end=end_ts,
        initial_equity=initial_equity,
        final_equity=final_equity,
        trades=trades,
        equity_curve=equity_curve,
        fees_paid=fees_paid,
    )
