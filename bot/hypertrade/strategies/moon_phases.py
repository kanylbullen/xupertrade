"""Moon Phases Strategy — Pine v5 port.

Long entry at the start of a full moon (lunar day 13-15), exit at new moon
(lunar day 0-1). Fixed 5% stop loss and 10% take profit.

Source logic (verified byte-equivalent):
    lunarCycleDays = 29.530588853
    referenceNewMoon = 2000-01-06 00:00:00 UTC (946857600 seconds)
    daysSinceReference = (bar_time_ms / 1000 - reference_s) / 86400
    currentLunarDay = floor((daysSinceReference % 29.530588853) + 0.5)

    isFullMoon = lunarDay in [13, 14, 15]
    isNewMoon  = lunarDay in [0, 1]

    fullMoonStart = isFullMoon AND NOT prev_isFullMoon
    newMoonStart  = isNewMoon  AND NOT prev_isNewMoon

    Long entry: fullMoonStart AND flat
    Long exit:  newMoonStart  OR SL hit (5%) OR TP hit (10%)

    Long-only, no short side. Source default mode: 'Long Only (Research)'.
"""

import math

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register

# Jan 6 2000 00:00 UTC
_REFERENCE_NEW_MOON_S = 946_857_600.0
_LUNAR_CYCLE = 29.530588853


def _lunar_day(ts_ms: float) -> int:
    """Return lunar day 0-29 for a given UTC timestamp in milliseconds."""
    days_since = (ts_ms / 1000 - _REFERENCE_NEW_MOON_S) / 86_400
    return int((days_since % _LUNAR_CYCLE) + 0.5) % 30


@register
class MoonPhasesStrategy(Strategy):
    name = "moon_phases"
    symbol = "BTC"
    timeframe = "1d"
    leverage = 1

    stop_loss_pct: float = 5.0
    take_profit_pct: float = 10.0

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False
        self._sl: float | None = None
        self._tp: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_position = True
        self._sl = entry_price * (1 - self.stop_loss_pct / 100)
        self._tp = entry_price * (1 + self.take_profit_pct / 100)

    def export_state(self) -> dict | None:
        if not self._in_position:
            return None
        return {
            "in_position": self._in_position,
            "sl": self._sl,
            "tp": self._tp,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_position = bool(state.get("in_position", True))
        self._sl = state.get("sl")
        self._tp = state.get("tp")

    def reset_state(self) -> None:
        self._in_position = False
        self._sl = None
        self._tp = None

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        if len(candles) < 35:
            return None

        # Runner already strips the forming bar before calling us
        # (runner.py:434), so `candles.iloc[-1]` IS the latest closed bar.
        # Pre-2026-05-09 we double-stripped here, delaying every signal
        # by one full bar (1d on this strategy → missing the lunar
        # transition cusp). Use candles directly. (Audit M5.)
        latest = candles.iloc[-1]
        prev = candles.iloc[-2]

        # Get timestamps in ms
        ts_latest = float(latest["timestamp"].timestamp() * 1000) if hasattr(latest["timestamp"], "timestamp") else float(latest["timestamp"])
        ts_prev = float(prev["timestamp"].timestamp() * 1000) if hasattr(prev["timestamp"], "timestamp") else float(prev["timestamp"])

        ld_cur = _lunar_day(ts_latest)
        ld_prev = _lunar_day(ts_prev)

        is_full_cur = 13 <= ld_cur <= 15
        is_full_prev = 13 <= ld_prev <= 15
        is_new_cur = ld_cur in (0, 1)
        is_new_prev = ld_prev in (0, 1)

        full_moon_start = is_full_cur and not is_full_prev
        new_moon_start = is_new_cur and not is_new_prev

        close = float(latest["close"])
        # SL/TP comparisons use the latest closed bar's high/low. Since
        # the runner has already stripped the forming bar, this is the
        # freshest data available — the "live = candles.iloc[-1]" alias
        # we used post-PR-15 was misleading (audit C1) and was the same
        # bar as latest in production. Rename for clarity.
        bar_high = float(latest["high"])
        bar_low = float(latest["low"])

        # ---- Manage open position ----
        if self._in_position and self._sl is not None and self._tp is not None:
            if bar_low <= self._sl:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: low ${bar_low:,.2f} <= ${self._sl:,.2f} (lunar day {ld_cur})",
                )
            if bar_high >= self._tp:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"TP hit: high ${bar_high:,.2f} >= ${self._tp:,.2f} (lunar day {ld_cur})",
                )
            if new_moon_start:
                self._in_position = False
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"New moon exit: lunar day {ld_cur}, close ${close:,.2f}",
                )
            return None

        # ---- Entry: full moon start ----
        if full_moon_start:
            self._in_position = True
            self._sl = close * (1 - self.stop_loss_pct / 100)
            self._tp = close * (1 + self.take_profit_pct / 100)
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Full moon start: lunar day {ld_cur} (prev {ld_prev}), "
                    f"close ${close:,.2f}. SL ${self._sl:,.2f} TP ${self._tp:,.2f}"
                ),
            )

        return None
