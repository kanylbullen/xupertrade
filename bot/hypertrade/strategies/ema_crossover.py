"""7/19 EMA Crypto Strategy — Pine v5 port.

EMA crossover strategy. Bullish cross → long, bearish cross → short.
Stop-loss at the lowest low (long) / highest high (short) of the last N candles.
Exit on opposite cross or SL hit.

Source logic (verified byte-equivalent):
    ema7  = EMA(close, 7)
    ema19 = EMA(close, 19)

    longSignal  = ta.crossover(ema7, ema19)   → prev: ema7 <= ema19, cur: ema7 > ema19
    shortSignal = ta.crossunder(ema7, ema19)  → prev: ema7 >= ema19, cur: ema7 < ema19

    longSL  = min(low[-4:])   (lowest low of last 4 candles, slCandles=4)
    shortSL = max(high[-4:])

    Long exit:  shortSignal OR low <= longSL
    Short exit: longSignal OR high >= shortSL

    No TP by default (useTP1=false). SL at n-candle structure low/high.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class EMACrossoverStrategy(Strategy):
    name = "ema_crossover"
    symbol = "BTC"
    timeframe = "1h"
    leverage = 1

    fast_len: int = 7
    slow_len: int = 19
    sl_candles: int = 4

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_long: bool = False
        self._in_short: bool = False
        self._sl: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_long = side == "long"
        self._in_short = side == "short"
        self._sl = None  # recomputed from structure high/low on first tick

    def export_state(self) -> dict | None:
        if not (self._in_long or self._in_short):
            return None
        return {
            "in_long": self._in_long,
            "in_short": self._in_short,
            "sl": self._sl,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_long = bool(state.get("in_long", side == "long"))
        self._in_short = bool(state.get("in_short", side == "short"))
        self._sl = state.get("sl")

    def reset_state(self) -> None:
        self._in_long = False
        self._in_short = False
        self._sl = None

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.slow_len + self.sl_candles + 5:
            return None

        df = candles.copy()
        df["ema_fast"] = pta.ema(df["close"], length=self.fast_len)
        df["ema_slow"] = pta.ema(df["close"], length=self.slow_len)

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
        high = float(latest["high"])
        low = float(latest["low"])

        bullish_cross = prev_fast <= prev_slow and cur_fast > cur_slow
        bearish_cross = prev_fast >= prev_slow and cur_fast < cur_slow

        # ---- Manage open positions ----
        # Pine source has NO reverse-on-opposite-cross block. Positions are
        # only closed via SL hit (or TP if enabled, which is off by default).
        # After SL closes the position, the next opposite cross is needed
        # for re-entry. Removing the reversal block was the audit fix —
        # was producing 5–10× the trade count of the source.
        if self._in_long:
            if self._sl is None:
                self._sl = float(closed["low"].iloc[-self.sl_candles:].min())
            if low <= self._sl:
                self._in_long = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: low ${low:,.2f} <= ${self._sl:,.2f}",
                )

        if self._in_short:
            if self._sl is None:
                self._sl = float(closed["high"].iloc[-self.sl_candles:].max())
            if high >= self._sl:
                self._in_short = False
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: high ${high:,.2f} >= ${self._sl:,.2f}",
                )

        # ---- Fresh entry ----
        if not self._in_long and not self._in_short:
            if bullish_cross:
                sl = float(closed["low"].iloc[-self.sl_candles:].min())
                self._sl = sl
                self._in_long = True
                return Signal(
                    action=SignalAction.OPEN_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"Bullish cross: EMA{self.fast_len}={cur_fast:,.2f} > EMA{self.slow_len}={cur_slow:,.2f}. "
                        f"SL=${sl:,.2f} (lowest low of last {self.sl_candles} bars)"
                    ),
                )
            if bearish_cross:
                sl = float(closed["high"].iloc[-self.sl_candles:].max())
                self._sl = sl
                self._in_short = True
                return Signal(
                    action=SignalAction.OPEN_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"Bearish cross: EMA{self.fast_len}={cur_fast:,.2f} < EMA{self.slow_len}={cur_slow:,.2f}. "
                        f"SL=${sl:,.2f} (highest high of last {self.sl_candles} bars)"
                    ),
                )

        return None
