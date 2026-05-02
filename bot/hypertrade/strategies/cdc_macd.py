"""CDC EMA Crossover (MACD) Strategy — Pine v5 port.

Simple EMA 12/26 crossover, long only. Equivalent to MACD crossing zero.

Source logic (verified byte-equivalent):
    emaFast = EMA(close, 12)
    emaSlow = EMA(close, 26)

    buySignal  = ta.crossover(emaFast, emaSlow)   → long entry
    sellSignal = ta.crossunder(emaFast, emaSlow)  → close long

    Long-only, no stop loss, no take profit. Full equity sizing.
    Source: CDC Backtest (MACD) with fixed $200k per trade (scaled).
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class CDCMACDStrategy(Strategy):
    name = "cdc_macd"
    symbol = "SOL"
    timeframe = "1d"
    leverage = 1

    ema_fast: int = 12
    ema_slow: int = 26

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = True

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.ema_slow + 5:
            return None

        df = candles.copy()
        df["ema_fast"] = pta.ema(df["close"], length=self.ema_fast)
        df["ema_slow"] = pta.ema(df["close"], length=self.ema_slow)

        closed = df.iloc[:-1]
        latest = closed.iloc[-1]
        prev = closed.iloc[-2]

        for col in ("ema_fast", "ema_slow"):
            if pd.isna(latest[col]) or pd.isna(prev[col]):
                return None

        cur_fast = float(latest["ema_fast"])
        cur_slow = float(latest["ema_slow"])
        prev_fast = float(prev["ema_fast"])
        prev_slow = float(prev["ema_slow"])
        close = float(latest["close"])

        buy_signal = prev_fast <= prev_slow and cur_fast > cur_slow
        sell_signal = prev_fast >= prev_slow and cur_fast < cur_slow

        if not self._in_position and buy_signal:
            self._in_position = True
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"EMA{self.ema_fast} crossed above EMA{self.ema_slow}: "
                    f"{cur_fast:,.2f} > {cur_slow:,.2f} (close ${close:,.2f})"
                ),
            )

        if self._in_position and sell_signal:
            self._in_position = False
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"EMA{self.ema_fast} crossed below EMA{self.ema_slow}: "
                    f"{cur_fast:,.2f} < {cur_slow:,.2f} (close ${close:,.2f})"
                ),
            )

        return None
