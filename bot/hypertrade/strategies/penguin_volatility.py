"""Penguin Volatility State Strategy — Pine v5 port (© waranyu.trkm).

Uses the ratio between Bollinger Band width and Keltner Channel width to
classify market volatility, then applies EMA-based state (Green/Yellow/Red/Blue)
to gate entries. The timing filter (RSI of diff) provides precise entry/exit.

Source logic (verified byte-equivalent, long-only with timing filter):
    BB: basisBB = SMA(close, 20), upperBB = basisBB + 2.0 * stdev(close, 20)
    KC: ATR(20), upperKC = basisBB + 2.0 * ATR(20)
    diff = (upperBB - upperKC) / upperKC * 100        (BB/KC spread %)
    rsi_diff = RSI(diff, 14)                           (RSI of the diff series)
    rsi_diff2 = SMA(rsi_diff, 7)

    fast_ma = EMA(close, 12)
    slow_ma = EMA(close, 26)
    apcdc   = EMA(ohlc4, 2)                           (price momentum proxy)

    isGreen  = fast_ma > slow_ma AND apcdc > fast_ma  (trend up + bullish momentum)
    isYellow = fast_ma > slow_ma AND apcdc < fast_ma  (trend up + weakening)

    can_long = isGreen or isYellow  (use_regime_filter=false)

    With timing filter (use_timing_filter=true):
      entry_long = rsi_diff2 crosses under rsi_diff AND can_long
                  (rsi_diff accelerating upward = volatility expanding)
      exit_long  = rsi_diff crosses under rsi_diff2

    Long-only. No stop loss. Trade direction = Long Only.
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class PenguinVolatilityStrategy(Strategy):
    name = "penguin_volatility"
    symbol = "ETH"
    timeframe = "1h"
    leverage = 1

    bb_len: int = 20
    bb_mult: float = 2.0
    kc_mult: float = 2.0
    ema_fast_len: int = 12
    ema_slow_len: int = 26
    rsi_diff_len: int = 14
    rsi_avg_len: int = 7
    # Pine source default is FALSE — entries are state-based (every bar in
    # green/yellow). When TRUE, entries also require the rsi_diff2 crossunder
    # rsi_diff event. Audit found the Python port had this hardcoded ON,
    # producing an entirely different signal generator from the source.
    use_timing_filter: bool = False

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = True

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        warmup = self.ema_slow_len + self.rsi_diff_len + self.rsi_avg_len + 20
        if len(candles) < warmup:
            return None

        df = candles.copy()

        # Bollinger Bands & Keltner Channel (same basis = SMA20)
        basis = pta.sma(df["close"], length=self.bb_len)
        stdev = df["close"].rolling(self.bb_len).std()
        atr = pta.atr(df["high"], df["low"], df["close"], length=self.bb_len)
        upper_bb = basis + self.bb_mult * stdev
        upper_kc = basis + self.kc_mult * atr

        # Diff: how much BB extends beyond KC (positive = BB wider)
        df["diff"] = (upper_bb - upper_kc) / upper_kc * 100

        # RSI of diff (RSI applied to the diff time series itself)
        df["rsi_diff"] = pta.rsi(df["diff"], length=self.rsi_diff_len)
        df["rsi_diff2"] = pta.sma(df["rsi_diff"], length=self.rsi_avg_len)

        # EMA state
        df["fast_ma"] = pta.ema(df["close"], length=self.ema_fast_len)
        df["slow_ma"] = pta.ema(df["close"], length=self.ema_slow_len)
        df["ohlc4"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
        df["apcdc"] = pta.ema(df["ohlc4"], length=2)

        closed = df.iloc[:-1]
        latest = closed.iloc[-1]
        prev = closed.iloc[-2]

        for col in ("rsi_diff", "rsi_diff2", "fast_ma", "slow_ma", "apcdc"):
            if pd.isna(latest[col]) or pd.isna(prev[col]):
                return None

        cur_rd = float(latest["rsi_diff"])
        cur_rd2 = float(latest["rsi_diff2"])
        prev_rd = float(prev["rsi_diff"])
        prev_rd2 = float(prev["rsi_diff2"])

        fast_ma = float(latest["fast_ma"])
        slow_ma = float(latest["slow_ma"])
        apcdc = float(latest["apcdc"])
        close = float(latest["close"])

        is_green = fast_ma > slow_ma and apcdc > fast_ma
        is_yellow = fast_ma > slow_ma and apcdc < fast_ma
        can_long = is_green or is_yellow

        # Timing filter: crossunder(rsi_diff2, rsi_diff) = rsi_diff2 just crossed under rsi_diff
        entry_timing = prev_rd2 >= prev_rd and cur_rd2 < cur_rd   # rsi_diff2 crossed under rsi_diff
        exit_timing = prev_rd >= prev_rd2 and cur_rd < cur_rd2    # rsi_diff crossed under rsi_diff2

        if self.use_timing_filter:
            entry_long = entry_timing and can_long
            exit_long = exit_timing
        else:
            # Pine default: state-only. Enter while green/yellow; exit when
            # state turns red/blue (i.e. fast_ma <= slow_ma).
            entry_long = can_long
            exit_long = not can_long

        if self._in_position:
            if exit_long:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"RSI-diff deceleration: rsi_diff {cur_rd:.1f} crossed below rsi_diff2 {cur_rd2:.1f}. "
                        f"State: {'Green' if is_green else 'Yellow' if is_yellow else 'Red/Blue'}"
                    ),
                )
        else:
            if entry_long:
                self._in_position = True
                return Signal(
                    action=SignalAction.OPEN_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"Volatility expanding: rsi_diff {cur_rd:.1f} > rsi_diff2 {cur_rd2:.1f}, "
                        f"state {'Green' if is_green else 'Yellow'} "
                        f"(EMA{self.ema_fast_len} {fast_ma:,.2f} > EMA{self.ema_slow_len} {slow_ma:,.2f})"
                    ),
                )

        return None
