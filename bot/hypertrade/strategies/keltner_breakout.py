"""ETHUSDT 4H Keltner Breakout — Pine v5 port.

Long-only: enter when close breaks above upper Keltner Channel AND price
is above EMA(200). Exit via ATR-based stop loss, 20% take-profit, or
close below lower KC (trend weakness).

Source logic (verified byte-equivalent, ETHUSDT 4H context):
    ema200    = EMA(close, 200)
    atr       = ATR(14)
    middleKC  = EMA(close, 20)
    upperKC   = middleKC + atr * 2.0
    lowerKC   = middleKC - atr * 2.0

    trendUp   = close > ema200
    breakout  = close > upperKC
    longCond  = trendUp AND breakout AND flat

    Stop loss: entry - ATR * 4.0           (ATR at entry time)
    Take profit: entry * 1.20              (20%)
    KC exit:    close < lowerKC            (trend weakness)
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class KeltnerBreakoutStrategy(Strategy):
    name = "keltner_breakout"
    symbol = "ETH"
    timeframe = "4h"
    leverage = 1

    ema_len: int = 200
    kc_len: int = 20
    atr_len: int = 14
    kc_mult: float = 2.0
    sl_atr_mult: float = 4.0
    tp_pct: float = 0.20   # 20%

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False
        self._entry: float = 0.0
        self._sl: float | None = None
        self._tp: float = 0.0

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = True
        self._entry = entry_price
        self._sl = None  # recomputed from ATR on first tick
        self._tp = entry_price * (1 + self.tp_pct)

    def export_state(self) -> dict | None:
        if not self._in_position:
            return None
        return {
            "in_position": self._in_position,
            "entry": self._entry,
            "sl": self._sl,
            "tp": self._tp,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_position = bool(state.get("in_position", True))
        self._entry = state.get("entry", entry_price)
        self._sl = state.get("sl")
        self._tp = state.get("tp", entry_price * (1 + self.tp_pct))

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < self.ema_len + 20:
            return None

        df = candles.copy()
        df["ema200"] = pta.ema(df["close"], length=self.ema_len)
        atr_series = pta.atr(df["high"], df["low"], df["close"], length=self.atr_len)
        df["atr"] = atr_series
        df["kc_mid"] = pta.ema(df["close"], length=self.kc_len)
        df["kc_upper"] = df["kc_mid"] + df["atr"] * self.kc_mult
        df["kc_lower"] = df["kc_mid"] - df["atr"] * self.kc_mult

        closed = df.iloc[:-1]
        latest = closed.iloc[-1]

        for col in ("ema200", "atr", "kc_upper", "kc_lower"):
            if pd.isna(latest[col]):
                return None

        close = float(latest["close"])
        high = float(latest["high"])
        low = float(latest["low"])
        ema200 = float(latest["ema200"])
        atr = float(latest["atr"])
        kc_upper = float(latest["kc_upper"])
        kc_lower = float(latest["kc_lower"])

        # ---- Manage open position ----
        if self._in_position:
            if self._sl is None:
                self._sl = self._entry - atr * self.sl_atr_mult
            if low <= self._sl:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: low ${low:,.2f} <= ${self._sl:,.2f}",
                )
            if high >= self._tp:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"TP hit: high ${high:,.2f} >= ${self._tp:,.2f} (20%)",
                )
            if close < kc_lower:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"KC exit: close ${close:,.2f} < lower KC ${kc_lower:,.2f}",
                )
            return None

        # ---- Entry ----
        trend_up = close > ema200
        breakout = close > kc_upper
        if trend_up and breakout:
            self._in_position = True
            self._entry = close
            self._sl = close - atr * self.sl_atr_mult
            self._tp = close * (1 + self.tp_pct)
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"KC breakout: close ${close:,.2f} > upper KC ${kc_upper:,.2f}, "
                    f"EMA200 ${ema200:,.2f}. SL ${self._sl:,.2f} TP ${self._tp:,.2f}"
                ),
            )

        return None
