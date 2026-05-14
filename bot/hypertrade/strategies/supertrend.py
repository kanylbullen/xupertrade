"""SuperTrend AI Strategy [Adaptive] — Pine v6 port (© DefinedEdge).

Direct Python port of the TradingView source. Adaptive SuperTrend with
regime-aware multiplier, AI signal scoring, multiple filters, and full
SL/TP risk management.

Pipeline per bar:
    1. Compute ATR(10) and ADX(14).
    2. Detect regime from atrRatio = atr / SMA(atr, 40) and ADX:
         - Volatile (2): atrRatio > 1.4
         - Ranging  (0): ADX < threshold AND atrRatio < 0.9
         - Trending (1): otherwise
    3. Adaptive multiplier:
         - Volatile: base × (1 + (atrRatio-1) × 0.4)
         - Ranging:  base × 0.85
         - Trending: base
       Clamped to [base × 0.5, base × 2.0].
    4. SuperTrend bands using hl2 ± adaptiveMult × ATR with the standard
       lower-floor / upper-ceiling continuity logic.
    5. On a trend flip, score the signal (0-100) across 5 factors:
         volume surge, displacement, EMA alignment, regime, prior band distance.
    6. Entry only if score ≥ minSignalScore (65) AND trend/regime/volume filters pass.
    7. SL = entry ± ATR × 6. TP = entry ± SL_distance × 2.5 (1:2.5 RR).

Source: 80% of equity, no leverage. Set leverage = 1 to mirror.
"""

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register

logger = logging.getLogger(__name__)


@register
class SuperTrendStrategy(Strategy):
    name = "supertrend"
    symbol = "BTC"
    timeframe = "1d"
    leverage = 1

    # SuperTrend
    atr_length: int = 10
    base_mult: float = 3.0

    # Regime
    regime_lookback: int = 40
    adx_length: int = 14
    adx_threshold: float = 20.0
    adaptive: bool = True

    # AI scoring
    trend_ema_length: int = 50
    volume_ma_length: int = 20
    min_signal_score: int = 65

    # Risk management
    sl_atr_mult: float = 6.0
    tp_rr: float = 2.5
    use_trail: bool = False
    trail_atr_mult: float = 2.5

    # Filters
    require_trend_alignment: bool = True
    skip_ranging: bool = True
    require_volume_spike: bool = True
    cooldown_bars: int = 5

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._position_side: str | None = None
        self._entry_price: float | None = None
        self._stop_loss: float | None = None
        self._take_profit: float | None = None
        self._trail_extreme: float | None = None
        self._trail_offset: float | None = None
        self._last_entry_time: datetime | None = None
        self._last_dir: int | None = None  # remembered ST direction across calls

    def restore_state(self, side: str, entry_price: float) -> None:
        self._position_side = side
        self._entry_price = entry_price
        self._stop_loss = None  # SuperTrend trailing SL recomputed on first tick

    def export_state(self) -> dict | None:
        if self._position_side is None:
            return None
        return {
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "trail_extreme": self._trail_extreme,
            "trail_offset": self._trail_offset,
            "last_entry_time": (
                self._last_entry_time.isoformat()
                if self._last_entry_time is not None
                else None
            ),
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._position_side = state.get("position_side", side)
        self._entry_price = state.get("entry_price", entry_price)
        self._stop_loss = state.get("stop_loss")
        self._take_profit = state.get("take_profit")
        self._trail_extreme = state.get("trail_extreme")
        self._trail_offset = state.get("trail_offset")
        last_entry_time = state.get("last_entry_time")
        if last_entry_time is not None:
            try:
                self._last_entry_time = datetime.fromisoformat(last_entry_time)
            except (ValueError, TypeError):
                self._last_entry_time = None
        else:
            self._last_entry_time = None

    def _reset_position_state(self) -> None:
        self._position_side = None
        self._entry_price = None
        self._stop_loss = None
        self._take_profit = None
        self._trail_extreme = None
        self._trail_offset = None

    def reset_state(self) -> None:
        self._reset_position_state()

    def _compute_adaptive_supertrend(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute regime, adaptive multiplier, and SuperTrend bands/direction."""
        atr = pta.atr(df["high"], df["low"], df["close"], length=self.atr_length)
        df["atr"] = atr
        atr_ma = atr.rolling(self.regime_lookback, min_periods=1).mean()
        df["atr_ratio"] = (atr / atr_ma).fillna(1.0)

        adx_df = pta.adx(df["high"], df["low"], df["close"], length=self.adx_length)
        adx_col = f"ADX_{self.adx_length}"
        df["adx"] = adx_df[adx_col] if adx_df is not None and adx_col in adx_df.columns else 0.0

        # Regime per bar
        def _regime(row: pd.Series) -> int:
            ratio = row["atr_ratio"]
            adx = row["adx"]
            if ratio > 1.4:
                return 2
            if adx < self.adx_threshold and ratio < 0.9:
                return 0
            return 1

        df["regime"] = df.apply(_regime, axis=1).astype(int)

        # Adaptive multiplier per bar
        if self.adaptive:
            mult = self.base_mult * pd.Series(1.0, index=df.index)
            volatile_mask = df["regime"] == 2
            mult[volatile_mask] = self.base_mult * (1.0 + (df.loc[volatile_mask, "atr_ratio"] - 1.0) * 0.4)
            ranging_mask = df["regime"] == 0
            mult[ranging_mask] = self.base_mult * 0.85
            mult = mult.clip(lower=self.base_mult * 0.5, upper=self.base_mult * 2.0)
        else:
            mult = pd.Series(self.base_mult, index=df.index)
        df["adapt_mult"] = mult

        # SuperTrend bands (hl2 source) with stateful continuity
        src = (df["high"] + df["low"]) / 2
        upper_base = src + df["adapt_mult"] * df["atr"]
        lower_base = src - df["adapt_mult"] * df["atr"]

        st_band = pd.Series(float("nan"), index=df.index)
        st_dir = pd.Series(1, index=df.index)
        prev_band = float("nan")
        prev_dir = 1
        closes = df["close"].values
        ub = upper_base.values
        lb = lower_base.values

        for i in range(len(df)):
            if pd.isna(prev_band):
                cur_band = lb[i] if prev_dir == 1 else ub[i]
            else:
                cur_band = prev_band

            if prev_dir == 1:
                cur_band = max(lb[i], cur_band)
                if closes[i] < cur_band:
                    prev_dir = -1
                    cur_band = ub[i]
            else:
                cur_band = min(ub[i], cur_band)
                if closes[i] > cur_band:
                    prev_dir = 1
                    cur_band = lb[i]

            st_band.iloc[i] = cur_band
            st_dir.iloc[i] = prev_dir
            prev_band = cur_band

        df["st_band"] = st_band
        df["st_dir"] = st_dir
        return df

    def _score_signal(self, df: pd.DataFrame, is_bull: bool) -> int:
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        atr = float(latest["atr"]) or 0.001
        close = float(latest["close"])
        st_band = float(latest["st_band"])
        prev_close = float(prev["close"])
        prev_st_band = float(prev["st_band"])
        vol = float(latest["volume"])
        vol_ma = float(latest["vol_ma"]) or 1.0
        trend_ema = float(latest["trend_ema"])
        regime = int(latest["regime"])
        score = 0.0

        # Factor 1: Volume surge (0-20)
        v_rat = vol / vol_ma if vol_ma > 0 else 1.0
        if v_rat >= 2.5:
            score += 20
        elif v_rat >= 1.5:
            score += 14
        elif v_rat >= 1.0:
            score += 8
        else:
            score += 3

        # Factor 2: Displacement beyond band (0-25)
        disp = (close - st_band) if is_bull else (st_band - close)
        disp_atr = disp / atr if atr > 0 else 0
        if disp_atr >= 1.5:
            score += 25
        elif disp_atr >= 0.8:
            score += 18
        elif disp_atr >= 0.3:
            score += 12
        elif disp_atr > 0:
            score += 5

        # Factor 3: EMA trend alignment + distance (0-20)
        trend_up = close > trend_ema
        trend_dn = close < trend_ema
        aligned = (is_bull and trend_up) or (not is_bull and trend_dn)
        ema_dist = abs(close - trend_ema) / atr
        if aligned and ema_dist > 0.5:
            score += 20
        elif aligned:
            score += 14
        elif ema_dist < 0.3:
            score += 8
        else:
            score += 2

        # Factor 4: Regime quality (0-15)
        if regime == 1:
            score += 15
        elif regime == 2:
            score += 8
        else:
            score += 3

        # Factor 5: Band distance before flip (0-20)
        prev_dist = abs(prev_close - prev_st_band) / atr if not pd.isna(prev_st_band) else 0
        if prev_dist >= 2.0:
            score += 20
        elif prev_dist >= 1.0:
            score += 14
        elif prev_dist >= 0.5:
            score += 8
        else:
            score += 3

        return int(min(round(score), 100))

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # Need enough history for the longest indicator (regime_lookback or trend_ema_length)
        warmup = max(self.regime_lookback, self.trend_ema_length, self.adx_length) + 10
        if len(candles) < warmup:
            return None

        df = candles.copy()
        latest = df.iloc[-1]
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
        # Use the LAST CLOSED bar (iloc[-2]) for trailing-SL updates and SL/TP
        # hit checks. The live (in-progress) bar at iloc[-1] has high/low
        # spanning all ticks since the bar opened — including ticks BEFORE a
        # mid-bar entry — which would otherwise instantly ratchet the trail
        # and stop us out on entry. See PR #127 (oleg_aryukov).
        if self._position_side is not None and self._entry_price is not None:
            if len(df) < 2:
                return None
            closed = df.iloc[-2]
            high = float(closed["high"])
            low = float(closed["low"])
            # After restore_state, SL and TP are None — lazily recompute
            # them from the current ATR. Best approximation since we don't
            # store entry-time ATR. Without this, restored positions run
            # unprotected until use_trail eventually sets SL.
            if self._stop_loss is None and self._take_profit is None:
                # Compute ATR locally (the strategy's main ATR is computed
                # later inside _compute_adaptive_supertrend; we need it here
                # before that runs).
                atr_series = pta.atr(
                    df["high"], df["low"], df["close"], length=self.atr_length
                )
                if atr_series is not None and not atr_series.empty:
                    atr_val = float(atr_series.iloc[-1])
                    if not pd.isna(atr_val) and atr_val > 0:
                        sl_dist = atr_val * self.sl_atr_mult
                        tp_dist = sl_dist * self.tp_rr
                        if self._position_side == "long":
                            self._stop_loss = self._entry_price - sl_dist
                            self._take_profit = self._entry_price + tp_dist
                        else:
                            self._stop_loss = self._entry_price + sl_dist
                            self._take_profit = self._entry_price - tp_dist
                        logger.info(
                            "[%s] Restored SL=%.2f TP=%.2f from current ATR (entry=%.2f, side=%s)",
                            self.name, self._stop_loss, self._take_profit,
                            self._entry_price, self._position_side,
                        )

            # Trailing stop update
            if self.use_trail and self._trail_offset is not None:
                if self._position_side == "long":
                    self._trail_extreme = max(self._trail_extreme or high, high)
                    trail_stop = self._trail_extreme - self._trail_offset
                    if self._stop_loss is None or trail_stop > self._stop_loss:
                        self._stop_loss = trail_stop
                else:
                    self._trail_extreme = min(self._trail_extreme or low, low)
                    trail_stop = self._trail_extreme + self._trail_offset
                    if self._stop_loss is None or trail_stop < self._stop_loss:
                        self._stop_loss = trail_stop

            sl = self._stop_loss
            tp = self._take_profit

            if self._position_side == "long":
                if sl is not None and low <= sl:
                    entry = self._entry_price
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_LONG, symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"SL hit at ${sl:,.2f} (entry ${entry:,.2f}, low ${low:,.2f})",
                    )
                if tp is not None and high >= tp:
                    entry = self._entry_price
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_LONG, symbol=self.symbol,
                        strategy_name=self.name, price=tp,
                        reason=f"TP filled at ${tp:,.2f} (entry ${entry:,.2f}, high ${high:,.2f})",
                    )
            else:  # short
                if sl is not None and high >= sl:
                    entry = self._entry_price
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT, symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"SL hit at ${sl:,.2f} (entry ${entry:,.2f}, high ${high:,.2f})",
                    )
                if tp is not None and low <= tp:
                    entry = self._entry_price
                    self._reset_position_state()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT, symbol=self.symbol,
                        strategy_name=self.name, price=tp,
                        reason=f"TP filled at ${tp:,.2f} (entry ${entry:,.2f}, low ${low:,.2f})",
                    )
            return None

        # ----- Flat: compute indicators and look for entry -----
        df = self._compute_adaptive_supertrend(df)
        df["trend_ema"] = pta.ema(df["close"], length=self.trend_ema_length)
        df["vol_ma"] = pta.sma(df["volume"], length=self.volume_ma_length)

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        for col in ("st_band", "st_dir", "trend_ema", "vol_ma", "atr", "adx", "regime"):
            if pd.isna(latest[col]) or pd.isna(prev[col]):
                return None

        cur_dir = int(latest["st_dir"])
        prev_dir = int(prev["st_dir"])
        trend_flip = cur_dir != prev_dir
        if not trend_flip:
            return None

        # Cooldown check (in bars)
        if self._last_entry_time is not None:
            # Approximate bar size from the data
            bar_seconds = (
                pd.Timestamp(latest["timestamp"]) - pd.Timestamp(prev["timestamp"])
            ).total_seconds() if "timestamp" in df.columns else 86400
            elapsed_bars = (bar_time - self._last_entry_time).total_seconds() / max(bar_seconds, 1)
            if elapsed_bars <= self.cooldown_bars:
                return None

        is_bull = cur_dir == 1
        score = self._score_signal(df, is_bull)
        if score < self.min_signal_score:
            return None

        # Filters
        trend_up = close > float(latest["trend_ema"])
        trend_dn = close < float(latest["trend_ema"])
        regime = int(latest["regime"])
        vol = float(latest["volume"])
        vol_ma = float(latest["vol_ma"])

        if self.require_trend_alignment:
            if is_bull and not trend_up:
                return None
            if not is_bull and not trend_dn:
                return None
        if self.skip_ranging and regime == 0:
            return None
        if self.require_volume_spike and not (vol > vol_ma):
            return None

        # Build SL / TP / trail offset
        atr = float(latest["atr"])
        sl_dist = atr * self.sl_atr_mult
        tp_dist = sl_dist * self.tp_rr
        trail_offset = atr * self.trail_atr_mult if self.use_trail else None

        if is_bull:
            self._position_side = "long"
            self._entry_price = close
            self._stop_loss = close - sl_dist
            self._take_profit = close + tp_dist
            self._trail_offset = trail_offset
            self._trail_extreme = close if self.use_trail else None
            self._last_entry_time = bar_time
            return Signal(
                action=SignalAction.OPEN_LONG, symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=self._stop_loss, take_profit=self._take_profit,
                reason=(
                    f"ST flip → bull (score {score}/100, regime "
                    f"{['ranging','trending','volatile'][regime]}, "
                    f"adapt mult {float(latest['adapt_mult']):.2f}, ADX {float(latest['adx']):.0f})"
                ),
            )
        else:
            self._position_side = "short"
            self._entry_price = close
            self._stop_loss = close + sl_dist
            self._take_profit = close - tp_dist
            self._trail_offset = trail_offset
            self._trail_extreme = close if self.use_trail else None
            self._last_entry_time = bar_time
            return Signal(
                action=SignalAction.OPEN_SHORT, symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=self._stop_loss, take_profit=self._take_profit,
                reason=(
                    f"ST flip → bear (score {score}/100, regime "
                    f"{['ranging','trending','volatile'][regime]}, "
                    f"adapt mult {float(latest['adapt_mult']):.2f}, ADX {float(latest['adx']):.0f})"
                ),
            )
