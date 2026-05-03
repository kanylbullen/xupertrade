"""Hash Supertrend — Pine v6 port (© Hash Capital Research).

Direct Python port. Plain SuperTrend (ATR=16, factor=3.11) with optional
time-of-day filter. On every bar:
    - Compute SuperTrend(factor, ATR_period) → (band, direction).
    - direction < 0 = bullish, direction > 0 = bearish.
    - longSignal  = direction changed bull this bar (was bear, now bull).
    - shortSignal = direction changed bear this bar (was bull, now bear).
    - Optional time filter: only trade between [startHour:startMinute,
      endHour:endMinute] in the bar timestamp's local clock (we use UTC
      since the bot's df timestamps are UTC). Sessions crossing midnight
      are supported.
    - On longSignal: strategy.entry("Long", strategy.long) — flip from
      short to long handled by the engine flip-detect.
    - On shortSignal: strategy.entry("Short", strategy.short).

The Pine source has NO explicit SL/TP and NO close conditions other than
the implicit reverse-on-flip. We mirror that: entries flip when ST flips.

All visual / color / glow / alert blocks are display-only and omitted.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class HashSupertrendStrategy(Strategy):
    name = "hash_supertrend"
    symbol = "BTC"
    timeframe = "1h"
    leverage = 1

    # Core SuperTrend
    atr_period: int = 16
    factor: float = 3.11

    # Time filter (off by default; mirrors source)
    use_time_filter: bool = False
    start_hour: int = 9
    start_minute: int = 30
    end_hour: int = 16
    end_minute: int = 0

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._position_side: str | None = None
        self._entry_price: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._position_side = side
        self._entry_price = entry_price

    def export_state(self) -> dict | None:
        if self._position_side is None:
            return None
        return {
            "position_side": self._position_side,
            "entry_price": self._entry_price,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._position_side = state.get("position_side", side)
        self._entry_price = state.get("entry_price", entry_price)

    def _reset(self) -> None:
        self._position_side = None
        self._entry_price = None

    def reset_state(self) -> None:
        self._reset()

    def _is_in_session(self, bar_time: datetime) -> bool:
        if not self.use_time_filter:
            return True
        cur = bar_time.hour * 60 + bar_time.minute
        start = self.start_hour * 60 + self.start_minute
        end = self.end_hour * 60 + self.end_minute
        if end > start:
            return start <= cur <= end
        # Session crosses midnight
        return cur >= start or cur <= end

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # SuperTrend needs at least atr_period bars + a few to flip
        warmup = self.atr_period + 5
        if len(candles) < warmup:
            return None

        df = candles.copy()
        st = pta.supertrend(
            df["high"], df["low"], df["close"],
            length=self.atr_period, multiplier=self.factor,
        )
        if st is None or st.empty:
            return None

        dir_col = next(
            (c for c in st.columns if c.startswith("SUPERTd_")), None
        )
        if dir_col is None:
            return None
        df["st_dir"] = st[dir_col]

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        if pd.isna(latest["st_dir"]) or pd.isna(prev["st_dir"]):
            return None

        cur_dir = int(latest["st_dir"])  # 1 = bull, -1 = bear (pta convention)
        prev_dir = int(prev["st_dir"])
        long_signal = cur_dir > 0 and prev_dir <= 0
        short_signal = cur_dir < 0 and prev_dir >= 0

        # Bar time for session filter
        ts = latest.get("timestamp")
        if isinstance(ts, datetime):
            bar_time = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            try:
                t = pd.Timestamp(ts).to_pydatetime()
                bar_time = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            except Exception:
                bar_time = datetime.now(timezone.utc)

        if not self._is_in_session(bar_time):
            return None

        close = float(latest["close"])

        if long_signal:
            # If currently short, engine flip-detect closes the short before
            # opening the long. If already long, runner dedup skips.
            if self._position_side == "long":
                return None
            self._position_side = "long"
            self._entry_price = close
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Supertrend flip BULLISH (factor {self.factor}, "
                    f"ATR {self.atr_period}) at ${close:,.2f}"
                ),
            )

        if short_signal:
            if self._position_side == "short":
                return None
            self._position_side = "short"
            self._entry_price = close
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Supertrend flip BEARISH (factor {self.factor}, "
                    f"ATR {self.atr_period}) at ${close:,.2f}"
                ),
            )

        return None
