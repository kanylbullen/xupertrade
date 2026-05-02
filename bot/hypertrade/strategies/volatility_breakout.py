"""Volume Breakout Strategy [Tables Fixed] — Pine v5 port.

Direct Python port of the TradingView source. Trades a Keltner Channel
breakout filtered by 200-EMA trend, ADX, RSI and a volume spike.
ATR-based stop loss with breakeven bump and a trailing stop.

Long entry:
    crossover(close, kc_upper) AND volume > sma(volume, 18)
    AND close > ema(close, 220) AND rsi(14) > 50 AND adx(14) > 20

Short entry:
    crossunder(close, kc_lower) AND volume > sma(volume, 18)
    AND close < ema(close, 220) AND rsi(14) < 50 AND adx(14) > 20

Where:
    kc_basis = ema(close, 22)
    kc_range = atr(10) * 2.0
    kc_upper / kc_lower = kc_basis ± kc_range

Exit (managed bar-by-bar inside the strategy):
    - Hard stop: entry ± atr(14) * 4
    - Breakeven: when high reaches entry × (1 + 1.5%) (long) the SL is
      bumped up to entry. Mirror for shorts.
    - Trailing stop: activates when high reaches entry × (1 + 3%) (long).
      Once active, trail point = highest_high − entry × 1%. The effective
      stop becomes max(hard_sl, trail). Mirror for shorts.

Cooldown: 4 hours between entries.
Direction: long-only by default in TV — but source supports both, so do we.

The Pine source uses 50% of equity at 2x leverage. Here we use the
runner's MAX_POSITION_SIZE_USD * leverage(2x) — equivalent risk profile
once the user sets MAX_POSITION_SIZE_USD = 50% of equity.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class VolatilityBreakoutStrategy(Strategy):
    name = "volatility_breakout"
    symbol = "ETH"
    timeframe = "1h"
    leverage = 2  # source: input.int(2, ...)

    # Strategy logic
    kc_len: int = 22
    kc_mult: float = 2.0
    atr_kc_len: int = 10
    use_trend_ema: bool = True
    ema_len: int = 220
    use_adx: bool = True
    adx_thresh: int = 20
    use_vol_filter: bool = True
    vol_len: int = 18
    rsi_len: int = 14

    # Risk management
    atr_sl_len: int = 14
    sl_multiplier: float = 4.0
    bk_activation_pct: float = 0.015  # 1.5%
    use_trail: bool = True
    trail_start_pct: float = 0.03  # 3%
    trail_offset_pct: float = 0.01  # 1%

    # Cooldown between entries
    cooldown_hours: int = 4

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._position_side: str | None = None
        self._entry_price: float | None = None
        self._stop_loss: float | None = None
        self._trail_active: bool = False
        self._trail_extreme: float | None = None  # max high (long) or min low (short)
        self._last_trade_time: datetime | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        self._position_side = side
        self._entry_price = entry_price
        self._stop_loss = None  # recomputed from ATR on first tick
        self._trail_active = False
        self._trail_extreme = None

    def export_state(self) -> dict | None:
        if self._position_side is None:
            return None
        return {
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "trail_active": self._trail_active,
            "trail_extreme": self._trail_extreme,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._position_side = state.get("position_side", side)
        self._entry_price = state.get("entry_price", entry_price)
        self._stop_loss = state.get("stop_loss")
        self._trail_active = bool(state.get("trail_active", False))
        self._trail_extreme = state.get("trail_extreme")

    def _reset_position_state(self) -> None:
        self._position_side = None
        self._entry_price = None
        self._stop_loss = None
        self._trail_active = False
        self._trail_extreme = None

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # Need enough history for the longest indicator (ema_len + a buffer)
        if len(candles) < self.ema_len + 5:
            return None

        df = candles.copy()
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])
        bar_time = latest["timestamp"] if "timestamp" in df.columns else df.index[-1]
        if not isinstance(bar_time, datetime):
            try:
                bar_time = pd.Timestamp(bar_time).to_pydatetime()
            except Exception:
                bar_time = datetime.now(timezone.utc)
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)

        # ----- Manage open position first -----
        if self._position_side is not None and self._entry_price is not None:
            entry = self._entry_price

            # Pine recomputes long_sl / short_sl every bar from current ATR
            # (NOT a latched value). Audit fix — Python previously latched
            # SL from entry-time ATR, which was tighter when ATR widened
            # post-entry but looser when ATR contracted.
            atr_series = pta.atr(df["high"], df["low"], df["close"], length=self.atr_sl_len)
            atr_val = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
            if atr_val > 0:
                if self._position_side == "long":
                    self._stop_loss = entry - atr_val * self.sl_multiplier
                else:
                    self._stop_loss = entry + atr_val * self.sl_multiplier

            # Breakeven bump (Pine: applies only on bars where condition is
            # true, NOT latched — though in practice with hard SL also moving
            # this is mostly equivalent to "tighter of the two" each bar).
            if self._position_side == "long" and high >= entry * (1 + self.bk_activation_pct):
                if self._stop_loss is None or self._stop_loss < entry:
                    self._stop_loss = entry
            elif self._position_side == "short" and low <= entry * (1 - self.bk_activation_pct):
                if self._stop_loss is None or self._stop_loss > entry:
                    self._stop_loss = entry

            # Update trailing extreme if trail is active
            if self._trail_active:
                if self._position_side == "long":
                    self._trail_extreme = max(self._trail_extreme or high, high)
                else:
                    self._trail_extreme = min(self._trail_extreme or low, low)

            # Check trail activation
            if self.use_trail and not self._trail_active:
                if self._position_side == "long" and high >= entry * (1 + self.trail_start_pct):
                    self._trail_active = True
                    self._trail_extreme = high
                elif self._position_side == "short" and low <= entry * (1 - self.trail_start_pct):
                    self._trail_active = True
                    self._trail_extreme = low

            # Effective stop = max(hard_sl, trail) for long, min for short
            effective_stop = self._stop_loss
            if self._trail_active and self._trail_extreme is not None:
                if self._position_side == "long":
                    trail_stop = self._trail_extreme - entry * self.trail_offset_pct
                    effective_stop = max(effective_stop or trail_stop, trail_stop)
                else:
                    trail_stop = self._trail_extreme + entry * self.trail_offset_pct
                    effective_stop = min(effective_stop or trail_stop, trail_stop)

            # Exit check
            if effective_stop is not None:
                if self._position_side == "long" and low <= effective_stop:
                    side = self._position_side
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_LONG,
                        symbol=self.symbol,
                        strategy_name=self.name,
                        reason=(
                            f"{'Trail' if self._trail_active else 'Stop'} hit at "
                            f"${effective_stop:,.2f} (entry ${entry:,.2f}, low ${low:,.2f})"
                        ),
                    )
                if self._position_side == "short" and high >= effective_stop:
                    side = self._position_side
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT,
                        symbol=self.symbol,
                        strategy_name=self.name,
                        reason=(
                            f"{'Trail' if self._trail_active else 'Stop'} hit at "
                            f"${effective_stop:,.2f} (entry ${entry:,.2f}, high ${high:,.2f})"
                        ),
                    )
            return None

        # ----- Flat: look for entry -----

        # Cooldown
        if self._last_trade_time is not None:
            if bar_time - self._last_trade_time < timedelta(hours=self.cooldown_hours):
                return None

        # Compute indicators
        df["kc_basis"] = pta.ema(df["close"], length=self.kc_len)
        kc_atr = pta.atr(df["high"], df["low"], df["close"], length=self.atr_kc_len)
        df["kc_range"] = kc_atr * self.kc_mult
        df["kc_upper"] = df["kc_basis"] + df["kc_range"]
        df["kc_lower"] = df["kc_basis"] - df["kc_range"]

        df["vol_avg"] = pta.sma(df["volume"], length=self.vol_len)

        adx_df = pta.adx(df["high"], df["low"], df["close"], length=14)
        adx_col = f"ADX_14"
        if adx_df is None or adx_col not in adx_df.columns:
            return None
        df["adx"] = adx_df[adx_col]

        df["rsi"] = pta.rsi(df["close"], length=self.rsi_len)
        df["trend_ema"] = pta.ema(df["close"], length=self.ema_len)
        df["atr_sl"] = pta.atr(df["high"], df["low"], df["close"], length=self.atr_sl_len)

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # NaN guard
        for col in ("kc_upper", "kc_lower", "vol_avg", "adx", "rsi", "trend_ema", "atr_sl"):
            if pd.isna(latest[col]) or pd.isna(prev[col]):
                return None

        kc_upper = float(latest["kc_upper"])
        kc_lower = float(latest["kc_lower"])
        prev_kc_upper = float(prev["kc_upper"])
        prev_kc_lower = float(prev["kc_lower"])
        prev_close = float(prev["close"])
        volume = float(latest["volume"])
        vol_avg = float(latest["vol_avg"])
        adx_val = float(latest["adx"])
        rsi_val = float(latest["rsi"])
        trend_ema = float(latest["trend_ema"])
        atr_val = float(latest["atr_sl"])

        # crossover/crossunder match Pine: ta.crossover(a,b) = a[1]<=b[1] AND a>b
        long_cross = prev_close <= prev_kc_upper and close > kc_upper
        short_cross = prev_close >= prev_kc_lower and close < kc_lower

        vol_cond = (volume > vol_avg) if self.use_vol_filter else True
        adx_cond = (adx_val > self.adx_thresh) if self.use_adx else True
        is_uptrend = (close > trend_ema) if self.use_trend_ema else True
        is_downtrend = (close < trend_ema) if self.use_trend_ema else True

        long_signal = long_cross and vol_cond and is_uptrend and rsi_val > 50 and adx_cond
        short_signal = short_cross and vol_cond and is_downtrend and rsi_val < 50 and adx_cond

        if long_signal:
            self._position_side = "long"
            self._entry_price = close
            self._stop_loss = close - atr_val * self.sl_multiplier
            self._trail_active = False
            self._trail_extreme = None
            self._last_trade_time = bar_time
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=self._stop_loss,
                reason=(
                    f"KC long breakout: close ${close:,.2f} > upper ${kc_upper:,.2f} "
                    f"(RSI {rsi_val:.0f}, ADX {adx_val:.0f}, vol {volume:,.0f}>{vol_avg:,.0f})"
                ),
            )

        if short_signal:
            self._position_side = "short"
            self._entry_price = close
            self._stop_loss = close + atr_val * self.sl_multiplier
            self._trail_active = False
            self._trail_extreme = None
            self._last_trade_time = bar_time
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=self._stop_loss,
                reason=(
                    f"KC short breakdown: close ${close:,.2f} < lower ${kc_lower:,.2f} "
                    f"(RSI {rsi_val:.0f}, ADX {adx_val:.0f}, vol {volume:,.0f}>{vol_avg:,.0f})"
                ),
            )

        return None
