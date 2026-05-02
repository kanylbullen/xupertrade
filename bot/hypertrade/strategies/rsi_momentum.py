"""RSI > 70 Buy / Exit on Cross Below 70 — Pine v6 port (© Boubizee).

Direct Python port of the TradingView source. Counter-intuitive momentum
continuation: buys when RSI crosses ABOVE 70 (signal of strength, not
exhaustion) and exits when RSI crosses back below 70.

Source logic (verified byte-equivalent):
    longCondition = rsi > 70 AND rsi[1] <= 70           # fresh cross above
    exitCondition = ta.crossunder(rsi, 70)              # = rsi < 70 AND rsi[1] >= 70

Long-only. No stop loss, no take profit, no filters. Source uses
strategy default sizing (no leverage). Symbol/timeframe come from the
Minara article context (BTCUSDT 4h) — the script itself works on any
chart.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class RSIMomentumStrategy(Strategy):
    name = "rsi_momentum"
    symbol = "BTC"
    timeframe = "4h"
    leverage = 1  # source has no leverage input

    rsi_length: int = 14
    rsi_level: float = 70.0

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.rsi_length + 5:
            return None

        rsi_series = pta.rsi(candles["close"], length=self.rsi_length)
        if rsi_series is None or len(rsi_series) < 2:
            return None

        cur = rsi_series.iloc[-1]
        prev = rsi_series.iloc[-2]
        if pd.isna(cur) or pd.isna(prev):
            return None

        # longCondition = rsi > 70 AND rsi[1] <= 70
        if prev <= self.rsi_level and cur > self.rsi_level:
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"RSI crossed above {self.rsi_level} ({prev:.1f} → {cur:.1f})",
            )

        # exitCondition = ta.crossunder(rsi, 70) = rsi < 70 AND rsi[1] >= 70
        if prev >= self.rsi_level and cur < self.rsi_level:
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"RSI crossed below {self.rsi_level} ({prev:.1f} → {cur:.1f})",
            )

        return None
