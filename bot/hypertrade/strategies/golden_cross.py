"""Golden Cross SMA 50/200 — Pine v2 port (© ChartArt 2016).

Direct Python port of the TradingView source. Classic trend-following:
go long on Golden Cross (SMA50 crosses above SMA200), close on Death
Cross (SMA50 crosses below SMA200). Long-only, no SL, no TP, no filters.

Source logic (verified byte-equivalent):
    bullish_cross = crossover(sma(close, 50), sma(close, 200))
    bearish_cross = crossunder(sma(close, 50), sma(close, 200))
    if bullish_cross: enter long
    if bearish_cross: close long

Symbol/timeframe defaults to HYPE 1d — backtested on HL's high-volume
non-BTC/ETH/SOL/VVV pairs. The strategy itself is symbol-agnostic.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


# @register  — disabled 2026-05-04 after 5y backtest. All 7 majors positive
# but lag buy-and-hold by 100-700pp. Late signal, low edge. Kept for history.
class GoldenCrossStrategy(Strategy):
    name = "golden_cross"
    symbol = "HYPE"
    timeframe = "1d"
    leverage = 1  # source has no leverage input

    sma_fast: int = 50
    sma_slow: int = 200

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = side == "long"

    def reset_state(self) -> None:
        self._in_position = False

    def export_state(self) -> dict | None:
        if not self._in_position:
            return None
        return {"in_position": self._in_position}

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_position = bool(state.get("in_position", side == "long"))

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.sma_slow + 5:
            return None

        sma_fast = pta.sma(candles["close"], length=self.sma_fast)
        sma_slow = pta.sma(candles["close"], length=self.sma_slow)
        if sma_fast is None or sma_slow is None or len(sma_fast) < 2:
            return None

        cur_fast = sma_fast.iloc[-1]
        prev_fast = sma_fast.iloc[-2]
        cur_slow = sma_slow.iloc[-1]
        prev_slow = sma_slow.iloc[-2]

        if pd.isna(cur_fast) or pd.isna(prev_fast) or pd.isna(cur_slow) or pd.isna(prev_slow):
            return None

        # crossover(fast, slow): prev_fast <= prev_slow AND cur_fast > cur_slow
        bullish_cross = prev_fast <= prev_slow and cur_fast > cur_slow
        # crossunder(fast, slow): prev_fast >= prev_slow AND cur_fast < cur_slow
        bearish_cross = prev_fast >= prev_slow and cur_fast < cur_slow

        if not self._in_position and bullish_cross:
            self._in_position = True
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Golden Cross: SMA{self.sma_fast} {cur_fast:,.2f} > "
                    f"SMA{self.sma_slow} {cur_slow:,.2f}"
                ),
            )

        if self._in_position and bearish_cross:
            self._in_position = False
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Death Cross: SMA{self.sma_fast} {cur_fast:,.2f} < "
                    f"SMA{self.sma_slow} {cur_slow:,.2f}"
                ),
            )

        return None
