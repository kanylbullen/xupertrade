"""Crypto LONG 10m Scalper BB+RSI+EMA+Fib — Pine v6 port.

Direct Python port of the TradingView source (© Saletrader1). A long-only
scalper that combines Bollinger Bands, RSI, EMA9/SMA21 trend, and a
Fibonacci-zone filter built from the most recent ~10-minute swing window.

Pipeline per closed bar (position must be flat to enter):
    1. Bollinger Bands (20, 2.0), RSI(14), EMA(9), SMA(21).
    2. Compute a Fibonacci 38.2 / 61.8 zone over the last 10 minutes
       (number of bars = round(600 / timeframe_seconds), min 3).
       inFibZone = swingLow + 0.382*range <= close <= swingLow + 0.618*range.
    3. Trend filter:
         trendDown = close < SMA21 AND SMA21 < SMA21[1]
         emaTurnUp = EMA9 > EMA9[1] AND EMA9[1] < EMA9[2]
    4. Entry conditions:
         buyUptrend   = NOT trendDown AND close < BBlower AND RSI < 20
                        AND emaTurnUp AND inFibZone
         buyDowntrend = trendDown AND inFibZone AND EMA9 > EMA9[1]
                        AND low[1] < low[2] AND low[1] < low      (valley)

Exit conditions (require minimum profit of 0.1% over avg entry):
    sellSignal = close > BBupper AND RSI > 81 AND EMA9 > SMA21
                 AND (EMA9-SMA21)/SMA21 <= deltaThresh (1%)
    timeLimit  = 10 minutes elapsed since entry
    exitLong   = (sellSignal OR timeLimit) AND haveProfit

NY-session filter and visual labels in the source are display-only and
omitted. Backtest also has no SL/TP — only the technical/time exit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class BBRsiScalperStrategy(Strategy):
    name = "bb_rsi_scalper"
    symbol = "BTC"
    timeframe = "15m"  # source uses 10m but HL's candleSnapshot doesn't support 10m
    leverage = 1

    # Indicator parameters (mirror source)
    bb_length: int = 20
    bb_mult: float = 2.0
    rsi_length: int = 14
    ema_length: int = 9
    sma_length: int = 21

    # Min profit required to allow exit
    min_profit_pct: float = 0.1  # %

    # EMA9~SMA21 imminent-cross delta threshold for technical exit
    delta_pct_in: float = 1.0  # %

    # Hold time limit in minutes
    hold_minutes: int = 10

    # RSI thresholds
    rsi_buy_level: float = 20.0
    rsi_sell_level: float = 81.0

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False
        self._entry_price: float | None = None
        self._entry_time: datetime | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        # Long-only strategy
        self._in_position = True
        self._entry_price = entry_price
        # Entry time unknown after restart — best-effort: assume "now" so the
        # 10-minute hold timer restarts from process restore (conservative:
        # the strategy will hold a bit longer rather than instant-close).
        self._entry_time = datetime.now(timezone.utc)

    def export_state(self) -> dict | None:
        if not self._in_position:
            return None
        return {
            "in_position": self._in_position,
            "entry_price": self._entry_price,
            "entry_time": (
                self._entry_time.isoformat() if self._entry_time else None
            ),
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_position = bool(state.get("in_position", True))
        self._entry_price = state.get("entry_price", entry_price)
        et = state.get("entry_time")
        if et:
            try:
                self._entry_time = datetime.fromisoformat(et)
            except (ValueError, TypeError):
                self._entry_time = datetime.now(timezone.utc)
        else:
            self._entry_time = datetime.now(timezone.utc)

    def _reset(self) -> None:
        self._in_position = False
        self._entry_price = None
        self._entry_time = None

    def _bar_time(self, latest_row: pd.Series) -> datetime:
        ts = latest_row.get("timestamp")
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        try:
            t = pd.Timestamp(ts).to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return t
        except Exception:
            return datetime.now(timezone.utc)

    def _tf_seconds(self) -> int:
        """Approximate seconds per bar from the timeframe string (e.g. '10m', '1h')."""
        tf = self.timeframe.strip().lower()
        if tf.endswith("m"):
            return int(tf[:-1]) * 60
        if tf.endswith("h"):
            return int(tf[:-1]) * 3600
        if tf.endswith("d"):
            return int(tf[:-1]) * 86400
        return 600

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # Need at least max(bb_length, sma_length, rsi_length) + a few bars
        warmup = max(self.bb_length, self.sma_length, self.rsi_length) + 5
        if len(candles) < warmup:
            return None

        df = candles.copy()
        bb = pta.bbands(df["close"], length=self.bb_length, std=self.bb_mult)
        if bb is None or bb.empty:
            return None
        # Tolerate either 2.0_2.0 or 2.0 column suffixes
        upper_col = next(
            (c for c in bb.columns if c.startswith("BBU_")), None
        )
        lower_col = next(
            (c for c in bb.columns if c.startswith("BBL_")), None
        )
        if upper_col is None or lower_col is None:
            return None
        df["bb_upper"] = bb[upper_col]
        df["bb_lower"] = bb[lower_col]
        df["rsi"] = pta.rsi(df["close"], length=self.rsi_length)
        df["ema9"] = pta.ema(df["close"], length=self.ema_length)
        df["sma21"] = pta.sma(df["close"], length=self.sma_length)

        # Fibonacci over last 10 minutes worth of bars
        tf_sec = self._tf_seconds()
        bars_fib = max(3, int(round(600.0 / tf_sec)))
        df["swing_high"] = df["high"].rolling(bars_fib, min_periods=1).max()
        df["swing_low"] = df["low"].rolling(bars_fib, min_periods=1).min()

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]

        # Required indicator values
        for col in ("bb_upper", "bb_lower", "rsi", "ema9", "sma21"):
            if pd.isna(latest[col]) or pd.isna(prev[col]):
                return None
        if pd.isna(prev2["ema9"]) or pd.isna(prev2["low"]):
            return None

        close = float(latest["close"])
        low = float(latest["low"])
        rsi_val = float(latest["rsi"])
        ema9 = float(latest["ema9"])
        ema9_prev = float(prev["ema9"])
        ema9_prev2 = float(prev2["ema9"])
        sma21 = float(latest["sma21"])
        sma21_prev = float(prev["sma21"])
        bb_upper = float(latest["bb_upper"])
        bb_lower = float(latest["bb_lower"])

        delta = (ema9 - sma21) / sma21 if sma21 != 0.0 else 0.0
        delta_thresh = self.delta_pct_in / 100.0

        swing_high = float(latest["swing_high"])
        swing_low = float(latest["swing_low"])
        valid_swing = swing_high > swing_low
        if valid_swing:
            rng = swing_high - swing_low
            fib38 = swing_low + rng * 0.382
            fib62 = swing_low + rng * 0.618
            fib_low = min(fib38, fib62)
            fib_high = max(fib38, fib62)
            in_fib_zone = fib_low <= close <= fib_high
        else:
            in_fib_zone = False

        bar_time = self._bar_time(latest)

        # ----- Manage open position first -----
        if self._in_position and self._entry_price is not None:
            avg_price = self._entry_price
            min_profit_frac = self.min_profit_pct / 100.0
            have_profit = (
                avg_price > 0 and close >= avg_price * (1.0 + min_profit_frac)
            )

            sell_signal = (
                close > bb_upper
                and rsi_val > self.rsi_sell_level
                and ema9 > sma21
                and delta <= delta_thresh
            )

            time_limit_reached = False
            if self._entry_time is not None:
                elapsed = bar_time - self._entry_time
                time_limit_reached = elapsed >= timedelta(minutes=self.hold_minutes)

            exit_by_signal = sell_signal and have_profit
            exit_by_time = time_limit_reached and have_profit

            if exit_by_signal or exit_by_time:
                reason = (
                    f"Tech sell (close ${close:,.2f}>BBU ${bb_upper:,.2f}, RSI {rsi_val:.1f}>81, "
                    f"delta {delta * 100:.2f}%) +profit"
                    if exit_by_signal
                    else f"Hold timeout {self.hold_minutes}m + profit (entry ${avg_price:,.2f}, close ${close:,.2f})"
                )
                self._reset()
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=reason,
                )
            return None

        # ----- Flat: look for long entry -----
        trend_down = close < sma21 and sma21 < sma21_prev
        ema_turn_up = ema9 > ema9_prev and ema9_prev < ema9_prev2

        buy_uptrend = (
            (not trend_down)
            and close < bb_lower
            and rsi_val < self.rsi_buy_level
            and ema_turn_up
            and in_fib_zone
        )

        # Valley in downtrend: low[1] < low[2] AND low[1] < low (current)
        prev_low = float(prev["low"])
        prev2_low = float(prev2["low"])
        valley_down = (
            trend_down
            and in_fib_zone
            and ema9 > ema9_prev
            and prev_low < prev2_low
            and prev_low < low
        )

        if buy_uptrend or valley_down:
            self._in_position = True
            self._entry_price = close
            self._entry_time = bar_time
            tag = "uptrend setup" if buy_uptrend else "downtrend valley"
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"BB+RSI+EMA+Fib long ({tag}): close ${close:,.2f}, "
                    f"BBL ${bb_lower:,.2f}, RSI {rsi_val:.1f}, EMA9 ${ema9:,.2f}, "
                    f"SMA21 ${sma21:,.2f}"
                ),
            )

        return None
