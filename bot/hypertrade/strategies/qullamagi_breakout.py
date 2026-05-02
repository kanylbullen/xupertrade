"""Qullamaggie MA Breakout L+S v2.5 — Pine v6 port (Loose / Intraday preset).

Multi-MA stacked-trend breakout with optional pullback entries, ADX/RSI/volume
filters, breakeven stop, partial scale-out, and MA-based trailing stop.
Long & short.

This port implements the **Loose (Intraday)** preset, which is the source's
default. The Strict (Daily) preset is intentionally NOT ported — it would
require carrying twice the branching logic and the live bot only runs one
preset at a time.

Loose preset specifics (vs Strict):
    - Trend "perfect order" only requires close > MA1 > MA2 > MA3
      (ignores MA4 / MA5 ordering and all-slope-aligned check).
    - Box / contraction filter is bypassed (baseOK=true).
    - Breakout is intra-bar (high > boxHi or low < boxLo) — no requirement
      for breakout-on-close.
    - Volume filter uses 1.1× SMA threshold (vs configurable for strict).

Pipeline per bar (long mirror; short symmetric):
    1. Compute 5 MAs (EMA10, EMA20, SMA50, SMA100, SMA200), ADX, RSI, ATR(14),
       volume SMA, trailing-stop EMA20.
    2. Compute box high/low over the last 10 bars (excluding current).
    3. Trend OK: close > MA1 > MA2 > MA3.
    4. Volume OK: volume > volSMA × 1.1.
    5. ADX OK: adxMinLong (20) <= ADX <= 50 (filter overheated).
    6. RSI OK: RSI < 70 (default disabled — useRSI=false).
    7. Cooldown OK: at least N bars since last flat-from-position transition.
    8. Wide-candle filter: candleRange <= ATR × 2.5.
    9. Breakout: high > boxHi (long), low < boxLo (short).
   10. OR pullback entry: price near pullback line (default 2nd MA / EMA20),
       bounce candle, recent breakout in lookback window, lower vol threshold.

Risk management:
    - Initial stop = trailLine × (1 - stopBufferPct/100). trailLine = EMA20.
    - Hard stop = entry × (1 ± hardStopPct/100). default 2.5%.
    - Effective stop = max(trailLine_stop, hard_stop) for longs,
                       min(trailLine_stop, hard_stop) for shorts.
    - Breakeven: once price reaches entry + (entry - initStop) × beActivateRR
      (RR=0.8), bump stop to entry × (1 + beOffsetPct/100).
    - Partial take-profit (scale-out): close scaleQtyPct (10%) at scaleRR×risk.
    - Time exit: optional, if losing after maxBarsInTrade bars.
    - Exit on close < effective stop (long) / close > effective stop (short).

Notes / deviations from Pine source:
    - **Date range filter**: SKIPPED — we run live, not backtest windows.
    - **Higher-timeframe trend filter (htfTrend)**: SKIPPED. Default
      `useHtfTrend=false` so this has no effect on the default config. Same for
      `shortRequireHTF`.
    - **Stats / debug tables, plots, plotshapes, alert_message strings**: SKIPPED.
    - **Partial scale-out**: emulated as a state flag only (we can't easily
      partial-close via a Signal — engine doesn't model fractional closes).
      Once price hits target1, we mark `scaledLong=True` so breakeven logic can
      activate sooner; we do NOT actually fire a partial close. Documented as
      a known divergence: live behavior differs from Pine's profit-taking step.
    - **Pyramiding=0** in source — already enforced (we only enter when flat).
    - **Position sizing (`posSizePct=20% of equity`)**: SKIPPED — sizing is
      handled by the engine's RISK_BUDGET / MAX_POSITION_SIZE_USD config.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class QullamagiBreakoutStrategy(Strategy):
    name = "qullamagi_breakout"
    symbol = "ETH"
    timeframe = "1h"
    leverage = 1

    # MAs
    len10: int = 10
    len20: int = 20
    len50: int = 50
    len100: int = 100
    len200: int = 200

    # ADX
    use_adx: bool = True
    adx_len: int = 14
    adx_min_long: float = 20.0
    adx_min_short: float = 25.0
    adx_max_entry: float = 50.0

    # RSI (off by default in source)
    use_rsi: bool = False
    rsi_len: int = 14
    rsi_ob_level: float = 70.0
    rsi_os_level: float = 30.0

    # Box / breakout
    base_len: int = 10
    base_atr_len: int = 14

    # Pullback
    use_pullback_entry: bool = True
    pullback_ma_choice: str = "2nd MA"  # "1st MA" / "2nd MA" / "3rd MA"
    pullback_lookback: int = 20
    require_bounce: bool = True
    pullback_atr_mult: float = 0.5

    # Volume
    use_vol_filter: bool = True
    vol_len: int = 20
    pullback_vol_mult: float = 0.8

    # Wide candle filter
    use_wide_candle_filt: bool = True
    max_breakout_atr: float = 2.5

    # Risk
    trail_ma_len: int = 20
    trail_ma_type: str = "EMA"
    stop_buffer_pct: float = 0.3
    use_scale_out: bool = True
    scale_rr: float = 1.0
    scale_qty_pct: float = 10.0

    # Hard stop
    use_hard_stop: bool = True
    hard_stop_pct: float = 2.5

    # Time exit
    use_time_exit: bool = False
    max_bars_in_trade: int = 48

    # Breakeven
    use_breakeven: bool = True
    be_activate_rr: float = 0.8
    be_offset_pct: float = 0.1

    # Direction & cooldown
    trade_long: bool = True
    trade_short: bool = True
    cooldown_bars: int = 3

    # Short-only filters
    short_min_consec_red: int = 2
    short_require_ma5_below: bool = False

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._position_side: str | None = None
        self._entry_price: float | None = None
        self._init_stop: float | None = None
        self._scaled: bool = False
        self._be_activated: bool = False
        self._bars_in_trade: int = 0
        self._bars_since_flat: int = 10_000  # large = not in cooldown

    # ----- state -----
    def restore_state(self, side: str, entry_price: float) -> None:
        self._position_side = side
        self._entry_price = entry_price
        # Best-effort init stop reconstruction: use hard_stop band
        if side == "long":
            self._init_stop = entry_price * (1 - self.hard_stop_pct / 100.0)
        else:
            self._init_stop = entry_price * (1 + self.hard_stop_pct / 100.0)
        self._scaled = False
        self._be_activated = False
        self._bars_in_trade = 0

    def export_state(self) -> dict | None:
        if self._position_side is None:
            return None
        return {
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "init_stop": self._init_stop,
            "scaled": self._scaled,
            "be_activated": self._be_activated,
            "bars_in_trade": self._bars_in_trade,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._position_side = state.get("position_side", side)
        self._entry_price = state.get("entry_price", entry_price)
        self._init_stop = state.get("init_stop")
        self._scaled = bool(state.get("scaled", False))
        self._be_activated = bool(state.get("be_activated", False))
        self._bars_in_trade = int(state.get("bars_in_trade", 0))
        if self._init_stop is None:
            self.restore_state(side, entry_price)

    def _reset(self) -> None:
        self._position_side = None
        self._entry_price = None
        self._init_stop = None
        self._scaled = False
        self._be_activated = False
        self._bars_in_trade = 0
        self._bars_since_flat = 0

    # ----- main logic -----
    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        warmup = max(self.len200, self.pullback_lookback, 50) + 5
        if len(candles) < warmup:
            return None

        df = candles.copy()
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(latest["close"])
        prev_close = float(prev["close"])
        high = float(latest["high"])
        low = float(latest["low"])
        open_ = float(latest["open"])
        volume = float(latest["volume"])

        # ----- compute indicators -----
        ma1 = pta.ema(df["close"], length=self.len10)
        ma2 = pta.ema(df["close"], length=self.len20)
        ma3 = pta.sma(df["close"], length=self.len50)
        # ma4/ma5 not needed for loose preset trend filter — only for short-MA5
        ma5 = pta.sma(df["close"], length=self.len200)

        atr_series = pta.atr(df["high"], df["low"], df["close"], length=self.base_atr_len)
        # ADX
        adx_df = pta.adx(df["high"], df["low"], df["close"], length=self.adx_len)
        adx_col = f"ADX_{self.adx_len}"
        adx_series = (
            adx_df[adx_col] if adx_df is not None and adx_col in adx_df.columns
            else pd.Series(50.0, index=df.index)
        )
        rsi_series = pta.rsi(df["close"], length=self.rsi_len)
        vol_sma = pta.sma(df["volume"], length=self.vol_len)
        if self.trail_ma_type == "EMA":
            trail_line_series = pta.ema(df["close"], length=self.trail_ma_len)
        else:
            trail_line_series = pta.sma(df["close"], length=self.trail_ma_len)

        for series in (ma1, ma2, ma3, ma5, atr_series, vol_sma, trail_line_series):
            if series is None or pd.isna(series.iloc[-1]):
                return None

        ma1_v = float(ma1.iloc[-1])
        ma2_v = float(ma2.iloc[-1])
        ma3_v = float(ma3.iloc[-1])
        ma5_v = float(ma5.iloc[-1])
        atr = float(atr_series.iloc[-1])
        adx_v = float(adx_series.iloc[-1]) if not pd.isna(adx_series.iloc[-1]) else 0.0
        vol_sma_v = float(vol_sma.iloc[-1])
        trail_line = float(trail_line_series.iloc[-1])

        rsi_v = float(rsi_series.iloc[-1]) if rsi_series is not None and not pd.isna(rsi_series.iloc[-1]) else 50.0

        # Box high/low excluding current bar (Pine: ta.highest(high, baseLen)[1])
        box_hi = float(df["high"].iloc[-(self.base_len + 1):-1].max())
        box_lo = float(df["low"].iloc[-(self.base_len + 1):-1].min())

        # ----- Manage existing position -----
        if self._position_side is not None and self._entry_price is not None:
            self._bars_in_trade += 1
            entry = self._entry_price
            init_stop = self._init_stop if self._init_stop is not None else (
                entry * (1 - self.hard_stop_pct / 100.0) if self._position_side == "long"
                else entry * (1 + self.hard_stop_pct / 100.0)
            )

            stop_line_long = trail_line * (1.0 - self.stop_buffer_pct / 100.0)
            stop_line_short = trail_line * (1.0 + self.stop_buffer_pct / 100.0)

            if self._position_side == "long":
                risk = entry - init_stop
                # Breakeven activation
                if self.use_breakeven and not self._be_activated and risk > 0:
                    be_target = entry + risk * self.be_activate_rr
                    if high >= be_target:
                        self._be_activated = True
                # Scale-out (state-only, no partial close emitted)
                if self.use_scale_out and not self._scaled and risk > 0:
                    target1 = entry + risk * self.scale_rr
                    if close >= target1:
                        self._scaled = True
                # Effective stop
                be_stop = entry * (1 + self.be_offset_pct / 100.0)
                final_stop = (
                    max(stop_line_long, be_stop) if self._be_activated else stop_line_long
                )
                hard_stop = entry * (1 - self.hard_stop_pct / 100.0)
                effective_stop = (
                    max(final_stop, hard_stop) if self.use_hard_stop else final_stop
                )
                # Time exit
                time_exit = (
                    self.use_time_exit
                    and self._bars_in_trade >= self.max_bars_in_trade
                    and close < entry
                )
                if close < effective_stop or time_exit:
                    self._reset()
                    reason = (
                        f"Time exit @${close:,.2f} (entry ${entry:,.2f})"
                        if time_exit
                        else f"Stop hit: close ${close:,.2f} < stop ${effective_stop:,.2f}"
                    )
                    return Signal(
                        action=SignalAction.CLOSE_LONG, symbol=self.symbol,
                        strategy_name=self.name, reason=reason,
                    )
            else:  # short
                risk = init_stop - entry
                if self.use_breakeven and not self._be_activated and risk > 0:
                    be_target = entry - risk * self.be_activate_rr
                    if low <= be_target:
                        self._be_activated = True
                if self.use_scale_out and not self._scaled and risk > 0:
                    target1 = entry - risk * self.scale_rr
                    if close <= target1:
                        self._scaled = True
                be_stop = entry * (1 - self.be_offset_pct / 100.0)
                final_stop = (
                    min(stop_line_short, be_stop) if self._be_activated else stop_line_short
                )
                hard_stop = entry * (1 + self.hard_stop_pct / 100.0)
                effective_stop = (
                    min(final_stop, hard_stop) if self.use_hard_stop else final_stop
                )
                time_exit = (
                    self.use_time_exit
                    and self._bars_in_trade >= self.max_bars_in_trade
                    and close > entry
                )
                if close > effective_stop or time_exit:
                    self._reset()
                    reason = (
                        f"Time exit @${close:,.2f} (entry ${entry:,.2f})"
                        if time_exit
                        else f"Stop hit: close ${close:,.2f} > stop ${effective_stop:,.2f}"
                    )
                    return Signal(
                        action=SignalAction.CLOSE_SHORT, symbol=self.symbol,
                        strategy_name=self.name, reason=reason,
                    )
            return None

        # ----- Flat: cooldown bookkeeping -----
        self._bars_since_flat += 1
        if self.cooldown_bars > 0 and self._bars_since_flat <= self.cooldown_bars:
            return None

        # ----- Trend (loose preset) -----
        trend_long = close > ma1_v and ma1_v > ma2_v and ma2_v > ma3_v
        trend_short = close < ma1_v and ma1_v < ma2_v and ma2_v < ma3_v
        # Pullback uses relaxed perfect-order (loose): ma1>ma2>ma3
        trend_long_pullback = ma1_v > ma2_v and ma2_v > ma3_v
        trend_short_pullback = ma1_v < ma2_v and ma2_v < ma3_v

        # Filters
        adx_ok_long = (not self.use_adx) or (self.adx_min_long <= adx_v <= self.adx_max_entry)
        adx_ok_short = (not self.use_adx) or (self.adx_min_short <= adx_v <= self.adx_max_entry)
        rsi_ok_long = (not self.use_rsi) or rsi_v < self.rsi_ob_level
        rsi_ok_short = (not self.use_rsi) or rsi_v > self.rsi_os_level

        vol_ok = (not self.use_vol_filter) or volume > vol_sma_v * 1.1
        vol_ok_pullback = (not self.use_vol_filter) or volume > vol_sma_v * self.pullback_vol_mult

        candle_range = high - low
        wide_ok = (not self.use_wide_candle_filt) or candle_range <= atr * self.max_breakout_atr

        # Short extra filter: consecutive red candles + (optional) close < MA5
        consec_red = 0
        for i in range(1, self.short_min_consec_red + 1):
            if i >= len(df):
                break
            if float(df["close"].iloc[-1 - i]) < float(df["open"].iloc[-1 - i]):
                consec_red += 1
        short_consec_red_ok = (
            self.short_min_consec_red == 0 or consec_red >= self.short_min_consec_red
        )
        short_below_ma5 = (not self.short_require_ma5_below) or close < ma5_v
        short_extra = short_consec_red_ok and short_below_ma5

        # Breakout (loose: intra-bar)
        breakout_up = high > box_hi and prev_close <= box_hi
        breakout_dn = low < box_lo and prev_close >= box_lo

        # Pullback line
        pb_line = ma1_v if self.pullback_ma_choice == "1st MA" else (
            ma3_v if self.pullback_ma_choice == "3rd MA" else ma2_v
        )

        # Recent breakout occurred in last `pullback_lookback` bars
        had_recent_breakout_up = (
            float(df["high"].iloc[-self.pullback_lookback:].max()) > box_hi
        )
        had_recent_breakout_dn = (
            float(df["low"].iloc[-self.pullback_lookback:].min()) < box_lo
        )

        # Pullback conditions
        proximity = self.pullback_atr_mult * atr / close if close > 0 else 0.0
        near_pb_long = (
            low <= pb_line * (1 + proximity) and low >= pb_line * (1 - proximity)
        )
        near_pb_short = (
            high >= pb_line * (1 - proximity) and high <= pb_line * (1 + proximity)
        )
        bounce_up = close > open_
        bounce_dn = close < open_
        pb_bounce_ok_long = (not self.require_bounce) or bounce_up
        pb_bounce_ok_short = (not self.require_bounce) or bounce_dn
        close_above_pb = close > pb_line
        close_below_pb = close < pb_line
        pullback_long_cond = (
            self.use_pullback_entry and near_pb_long and pb_bounce_ok_long
            and close_above_pb and had_recent_breakout_up
        )
        pullback_short_cond = (
            self.use_pullback_entry and near_pb_short and pb_bounce_ok_short
            and close_below_pb and had_recent_breakout_dn
        )

        # Final entry conditions (loose preset)
        enter_long_breakout = (
            self.trade_long and trend_long and breakout_up and vol_ok
            and wide_ok and adx_ok_long and rsi_ok_long
        )
        enter_short_breakout = (
            self.trade_short and trend_short and breakout_dn and vol_ok
            and wide_ok and adx_ok_short and rsi_ok_short and short_extra
        )
        enter_long_pullback = (
            self.trade_long and trend_long_pullback and pullback_long_cond
            and vol_ok_pullback and wide_ok and adx_ok_long and rsi_ok_long
        )
        enter_short_pullback = (
            self.trade_short and trend_short_pullback and pullback_short_cond
            and vol_ok_pullback and wide_ok and adx_ok_short and rsi_ok_short and short_extra
        )

        if enter_long_breakout or enter_long_pullback:
            entry_type = "BREAKOUT" if enter_long_breakout else "PULLBACK"
            init_stop = trail_line * (1.0 - self.stop_buffer_pct / 100.0)
            # Apply hard stop floor
            hard_stop = close * (1 - self.hard_stop_pct / 100.0)
            if self.use_hard_stop:
                init_stop = max(init_stop, hard_stop)
            self._position_side = "long"
            self._entry_price = close
            self._init_stop = init_stop
            self._scaled = False
            self._be_activated = False
            self._bars_in_trade = 0
            return Signal(
                action=SignalAction.OPEN_LONG, symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=init_stop,
                reason=(
                    f"Long {entry_type}: close ${close:,.2f}, ADX {adx_v:.1f}, "
                    f"box ${box_lo:,.2f}-${box_hi:,.2f}, init stop ${init_stop:,.2f}"
                ),
            )

        if enter_short_breakout or enter_short_pullback:
            entry_type = "BREAKOUT" if enter_short_breakout else "PULLBACK"
            init_stop = trail_line * (1.0 + self.stop_buffer_pct / 100.0)
            hard_stop = close * (1 + self.hard_stop_pct / 100.0)
            if self.use_hard_stop:
                init_stop = min(init_stop, hard_stop)
            self._position_side = "short"
            self._entry_price = close
            self._init_stop = init_stop
            self._scaled = False
            self._be_activated = False
            self._bars_in_trade = 0
            return Signal(
                action=SignalAction.OPEN_SHORT, symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=init_stop,
                reason=(
                    f"Short {entry_type}: close ${close:,.2f}, ADX {adx_v:.1f}, "
                    f"box ${box_lo:,.2f}-${box_hi:,.2f}, init stop ${init_stop:,.2f}"
                ),
            )

        return None
