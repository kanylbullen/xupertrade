"""50 & 200 SMA + RSI Average Strategy — Pine v6 port (© muratkbesiroglu).

Direct Python port of the TradingView source. Long-only trend-following
with a smoothed RSI filter. Stays in cash during weak-momentum periods.

Source logic (verified byte-equivalent):
    longEntryCond = close > sma50 AND close > sma200 AND rsiMa > 57 AND not inLong
    longExitCond  = close < sma50 AND rsiMa < 57 AND inLong

Where:
    sma50  = SMA(close, 50)
    sma200 = SMA(close, 200)
    rsi    = RSI(close, 21)
    rsiMa  = SMA(rsi, 9)

Long-only, single position. Source uses 84% of equity per trade with no
leverage input. Symbol/timeframe come from the Minara article context
(ETHUSDT 1d) — the script itself works on any chart.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class SMARSIStrategy(Strategy):
    name = "sma_rsi"
    symbol = "ETH"
    timeframe = "1d"
    leverage = 1  # source has no leverage input

    sma_fast: int = 50
    sma_slow: int = 200
    rsi_length: int = 21
    rsi_smooth: int = 9
    rsi_threshold: float = 57.0

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = True

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.sma_slow + 10:
            return None

        df = candles.copy()
        df["sma_fast"] = pta.sma(df["close"], length=self.sma_fast)
        df["sma_slow"] = pta.sma(df["close"], length=self.sma_slow)
        rsi_series = pta.rsi(df["close"], length=self.rsi_length)
        df["rsi"] = rsi_series
        df["rsi_ma"] = pta.sma(rsi_series, length=self.rsi_smooth)

        latest = df.iloc[-1]
        for col in ("sma_fast", "sma_slow", "rsi_ma"):
            if pd.isna(latest[col]):
                return None

        close = float(latest["close"])
        sma_fast = float(latest["sma_fast"])
        sma_slow = float(latest["sma_slow"])
        rsi_ma = float(latest["rsi_ma"])

        # Long entry: price above both SMAs AND smoothed RSI > 57
        if not self._in_position and close > sma_fast and close > sma_slow and rsi_ma > self.rsi_threshold:
            self._in_position = True
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Long: close ${close:,.2f} > SMA50 ${sma_fast:,.2f} & SMA200 ${sma_slow:,.2f}, "
                    f"RSI_MA {rsi_ma:.1f} > {self.rsi_threshold}"
                ),
            )

        # Long exit: price below SMA50 AND smoothed RSI < 57
        if self._in_position and close < sma_fast and rsi_ma < self.rsi_threshold:
            self._in_position = False
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Exit: close ${close:,.2f} < SMA50 ${sma_fast:,.2f}, "
                    f"RSI_MA {rsi_ma:.1f} < {self.rsi_threshold}"
                ),
            )

        return None
