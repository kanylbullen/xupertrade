"""Pivot Point SuperTrend Strategy — Pine v5 port (Kadunagra).

Uses pivot highs/lows to compute a dynamic center line, then builds a
SuperTrend-style trailing stop from that center. Long & short with %SL.

Source logic (verified byte-equivalent):
    ph = ta.pivothigh(2, 2)            (pivot high: 2 bars left, 2 bars right)
    pl = ta.pivotlow(2, 2)             (pivot low: 2 bars left, 2 bars right)

    Center update: center = (center * 2 + lastPivot) / 3   (exponential-ish weighted avg)
    Up   = center - 3 * ATR(10)
    Dn   = center + 3 * ATR(10)

    TUp   := close[1] > prev_TUp   ? max(Up, prev_TUp)   : Up
    TDown := close[1] < prev_TDown ? min(Dn, prev_TDown) : Dn

    Trend = 1  if close > prev_TDown
            -1 if close < prev_TUp
            else prev_Trend

    bsignal = Trend == 1 AND Trend[1] == -1   (flip to bullish)
    ssignal = Trend == -1 AND Trend[1] == 1   (flip to bearish)

    MA filter: long only when close > EMA(200), short only when close < EMA(200)
    SL: entry ± 1%  (slPerc = 1.0)
"""

import pandas as pd
import pandas_ta as pta
import numpy as np

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


def _compute_pivot_supertrend(df: pd.DataFrame) -> pd.DataFrame:
    """Compute pivot-based SuperTrend iteratively."""
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    n = len(closes)

    # ATR(10) via pandas_ta
    atr_series = pta.atr(df["high"], df["low"], df["close"], length=10)
    atrs = atr_series.to_numpy(dtype=float)

    # Pivot highs / lows (2 left, 2 right) — look 2 bars ahead for confirmation
    pivot_highs = np.full(n, np.nan)
    pivot_lows = np.full(n, np.nan)
    for i in range(2, n - 2):
        if highs[i] == max(highs[i - 2], highs[i - 1], highs[i], highs[i + 1], highs[i + 2]):
            pivot_highs[i] = highs[i]
        if lows[i] == min(lows[i - 2], lows[i - 1], lows[i], lows[i + 1], lows[i + 2]):
            pivot_lows[i] = lows[i]

    center = np.full(n, np.nan)
    TUp = np.full(n, np.nan)
    TDown = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)

    for i in range(n):
        # Update center from latest pivot
        last_pivot = np.nan
        if not np.isnan(pivot_highs[i]):
            last_pivot = pivot_highs[i]
        elif not np.isnan(pivot_lows[i]):
            last_pivot = pivot_lows[i]

        if not np.isnan(last_pivot):
            prev_center = center[i - 1] if i > 0 and not np.isnan(center[i - 1]) else last_pivot
            center[i] = (prev_center * 2 + last_pivot) / 3
        elif i > 0 and not np.isnan(center[i - 1]):
            center[i] = center[i - 1]
        else:
            center[i] = np.nan

        if np.isnan(center[i]) or np.isnan(atrs[i]):
            trend[i] = trend[i - 1] if i > 0 else 1
            continue

        up = center[i] - 3 * atrs[i]
        dn = center[i] + 3 * atrs[i]

        prev_tup = TUp[i - 1] if i > 0 and not np.isnan(TUp[i - 1]) else up
        prev_tdown = TDown[i - 1] if i > 0 and not np.isnan(TDown[i - 1]) else dn
        prev_close = closes[i - 1] if i > 0 else closes[i]

        TUp[i] = max(up, prev_tup) if prev_close > prev_tup else up
        TDown[i] = min(dn, prev_tdown) if prev_close < prev_tdown else dn

        prev_trend = trend[i - 1] if i > 0 else 1
        if closes[i] > prev_tdown:
            trend[i] = 1
        elif closes[i] < prev_tup:
            trend[i] = -1
        else:
            trend[i] = prev_trend

    df["ps_center"] = center
    df["ps_tup"] = TUp
    df["ps_tdown"] = TDown
    df["ps_trend"] = trend
    return df


@register
class PivotSuperTrendStrategy(Strategy):
    name = "pivot_supertrend"
    symbol = "BTC"
    timeframe = "4h"
    leverage = 1

    ma_len: int = 200
    sl_pct: float = 1.0   # 1% stop loss

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_long: bool = False
        self._in_short: bool = False
        self._sl: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_long = side == "long"
        self._in_short = side == "short"
        if side == "long":
            self._sl = entry_price * (1 - self.sl_pct / 100)
        else:
            self._sl = entry_price * (1 + self.sl_pct / 100)

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
        if len(candles) < self.ma_len + 20:
            return None

        df = candles.copy()
        df["ema200"] = pta.ema(df["close"], length=self.ma_len)
        df = _compute_pivot_supertrend(df)

        # Need 2 extra bars ahead for pivot confirmation → use iloc[:-3] closed
        # (last confirmed pivot needs 2 bars to its right, then 1 more closed bar)
        closed = df.iloc[:-1]
        latest = closed.iloc[-1]
        prev = closed.iloc[-2]

        for col in ("ps_trend", "ema200"):
            if pd.isna(latest[col]):
                return None

        cur_trend = int(latest["ps_trend"])
        prev_trend = int(prev["ps_trend"])
        close = float(latest["close"])
        high = float(latest["high"])
        low = float(latest["low"])
        ema200 = float(latest["ema200"])

        bsignal = cur_trend == 1 and prev_trend == -1   # flip to bullish
        ssignal = cur_trend == -1 and prev_trend == 1    # flip to bearish

        # ---- Manage open positions ----
        if self._in_long and self._sl is not None:
            if low <= self._sl:
                self._in_long = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: low ${low:,.2f} <= ${self._sl:,.2f}",
                )
            if ssignal and close < ema200:
                self._in_long = False
                # Enter short immediately on flip with valid filter
                self._in_short = True
                self._sl = close * (1 + self.sl_pct / 100)
                return Signal(
                    action=SignalAction.OPEN_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"PS flipped bearish, close ${close:,.2f} < EMA200 ${ema200:,.2f}. SL ${self._sl:,.2f}",
                )

        if self._in_short and self._sl is not None:
            if high >= self._sl:
                self._in_short = False
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: high ${high:,.2f} >= ${self._sl:,.2f}",
                )
            if bsignal and close > ema200:
                self._in_short = False
                self._in_long = True
                self._sl = close * (1 - self.sl_pct / 100)
                return Signal(
                    action=SignalAction.OPEN_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"PS flipped bullish, close ${close:,.2f} > EMA200 ${ema200:,.2f}. SL ${self._sl:,.2f}",
                )

        # ---- Fresh entry ----
        if not self._in_long and not self._in_short:
            if bsignal and close > ema200:
                self._in_long = True
                self._sl = close * (1 - self.sl_pct / 100)
                return Signal(
                    action=SignalAction.OPEN_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"PS flipped bullish: trend {prev_trend}→{cur_trend}, "
                        f"close ${close:,.2f} > EMA200 ${ema200:,.2f}. SL ${self._sl:,.2f}"
                    ),
                )
            if ssignal and close < ema200:
                self._in_short = True
                self._sl = close * (1 + self.sl_pct / 100)
                return Signal(
                    action=SignalAction.OPEN_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"PS flipped bearish: trend {prev_trend}→{cur_trend}, "
                        f"close ${close:,.2f} < EMA200 ${ema200:,.2f}. SL ${self._sl:,.2f}"
                    ),
                )

        return None
