"""MACD Zero-Line Strategy (Long Only) — Pine v6 port.

Enter long when MACD line crosses above zero. Exit when MACD crosses below zero.
MACD = EMA(12) − EMA(26). Zero-cross = equivalent to EMA12/EMA26 crossover.

Source logic (verified byte-equivalent):
    [macd, signal, hist] = ta.macd(close, 12, 26, 9)

    macdCrossUp   = ta.crossover(macd, 0)    → prev_macd <= 0 AND cur_macd > 0
    macdCrossDown = ta.crossunder(macd, 0)   → prev_macd >= 0 AND cur_macd < 0

    Long entry: macdCrossUp  AND flat
    Long exit:  macdCrossDown AND in position

    Long-only. No stop loss, no take profit. Full equity sizing.
    Source: BTC MACD Zero-Line Strategy (Long Only).
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class MACDZeroStrategy(Strategy):
    name = "macd_zero"
    symbol = "BTC"
    timeframe = "1d"
    leverage = 1

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = True

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.macd_slow + 15:
            return None

        df = candles.copy()
        macd_df = pta.macd(df["close"], fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        if macd_df is None:
            return None

        macd_col = next((c for c in macd_df.columns if c.startswith("MACD_") and not c.startswith("MACDs_") and not c.startswith("MACDh_")), None)
        if macd_col is None:
            return None

        df["macd"] = macd_df[macd_col]
        closed = df.iloc[:-1]
        latest = closed.iloc[-1]
        prev = closed.iloc[-2]

        if pd.isna(latest["macd"]) or pd.isna(prev["macd"]):
            return None

        cur_macd = float(latest["macd"])
        prev_macd = float(prev["macd"])
        close = float(latest["close"])

        cross_up = prev_macd <= 0 and cur_macd > 0
        cross_down = prev_macd >= 0 and cur_macd < 0

        if not self._in_position and cross_up:
            self._in_position = True
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"MACD crossed above 0: {prev_macd:.2f} → {cur_macd:.2f} (close ${close:,.2f})",
            )

        if self._in_position and cross_down:
            self._in_position = False
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"MACD crossed below 0: {prev_macd:.2f} → {cur_macd:.2f} (close ${close:,.2f})",
            )

        return None
