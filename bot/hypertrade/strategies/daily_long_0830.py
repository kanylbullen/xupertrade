"""Daily Long 08:30 → Exit 08:00 — Pine v5 port.

Trivial intraday calendar strategy. Enters long on the candle whose UTC
timestamp is exactly 08:30, exits long on the candle whose UTC timestamp
is exactly 08:00. Long-only, no SL/TP.

Source logic (verified byte-equivalent):
    currentHour   = hour(time, "UTC")
    currentMinute = minute(time, "UTC")
    enterLong = (currentHour == 8 and currentMinute == 30)
    exitLong  = (currentHour == 8 and currentMinute == 0)

Runs on a 15m timeframe so a single closed candle's timestamp can match
either 08:00 or 08:30 exactly. Long-only with `_in_position` tracking
to avoid spamming OPEN_LONG when re-entered or already long.
"""

import pandas as pd

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class DailyLong0830Strategy(Strategy):
    name = "daily_long_0830"
    symbol = "BTC"
    timeframe = "15m"
    leverage = 1

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = side == "long"

    def export_state(self) -> dict | None:
        if not self._in_position:
            return None
        return {"in_position": self._in_position}

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_position = bool(state.get("in_position", side == "long"))

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < 2:
            return None

        # Latest closed candle (mirror ema_crossover convention)
        closed = candles.iloc[:-1]
        latest = closed.iloc[-1]

        ts = latest["timestamp"]
        if hasattr(ts, "tz_convert"):
            ts_utc = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
        elif hasattr(ts, "astimezone"):
            from datetime import timezone as _tz
            ts_utc = ts.astimezone(_tz.utc) if ts.tzinfo else ts.replace(tzinfo=_tz.utc)
        else:
            # Numeric epoch ms fallback
            ts_utc = pd.Timestamp(ts, unit="ms", tz="UTC")

        hour = int(ts_utc.hour)
        minute = int(ts_utc.minute)
        close = float(latest["close"])

        enter_long = hour == 8 and minute == 30
        exit_long = hour == 8 and minute == 0

        # Exit takes priority (08:00 fires before 08:30 chronologically anyway)
        if self._in_position and exit_long:
            self._in_position = False
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"Exit at 08:00 UTC, close ${close:,.2f}",
            )

        if not self._in_position and enter_long:
            self._in_position = True
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"Enter at 08:30 UTC, close ${close:,.2f}",
            )

        return None
