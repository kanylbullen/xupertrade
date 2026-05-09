"""Hash Momentum Strategy — Pine v6 port.

Normalized momentum with EMA trend filter and ATR-dynamic threshold.
Long & short with fixed percentage stop-loss and risk-reward take-profit.

Source logic (verified byte-equivalent):
    mom0 = close - close[momLength]
    mom1 = mom0 - mom0[1]                          (acceleration)
    momStdev = stdev(mom0, momLength * 3)
    momNormalized = mom0 / momStdev if momStdev > 0 else 0
    dynamicThreshold = ATR(14) * momThreshold       (ATR × 2.25)
    ema = EMA(close, emaLength)

    longSignal  = mom0 > threshold AND mom1 > 0 AND momNorm > 0.5
                  AND close > close[1] AND close > ema AND flat AND not inCooldown
    shortSignal = mom0 < -threshold AND mom1 < 0 AND momNorm < -0.5
                  AND close < close[1] AND close < ema AND flat AND not inCooldown

    SL (long):  entry * (1 - stopLossPerc/100)     = entry * 0.978
    TP (long):  entry + risk * riskRewardRatio      = entry + risk * 2.5
    Cooldown:   6 bars after any trade close
"""

import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class HashMomentumStrategy(Strategy):
    name = "hash_momentum"
    symbol = "SOL"
    timeframe = "4h"
    leverage = 1

    mom_length: int = 13
    mom_threshold_atr_mult: float = 2.25
    ema_length: int = 28
    stop_loss_pct: float = 2.2    # percent
    risk_reward: float = 2.5
    cooldown_bars: int = 6

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_long: bool = False
        self._in_short: bool = False
        self._entry: float | None = None
        self._sl: float | None = None
        self._tp: float | None = None
        self._bars_since_close: int = 999
        # Track the last fully-closed bar's timestamp so we only advance
        # `_bars_since_close` when a NEW bar closes — not on every tick.
        # Without this, a 6-bar cooldown on a 4h strategy expires in 6
        # minutes (60s polling × 6 ticks), defeating the purpose and
        # causing rapid-fire re-entry loops on stale bar data. See
        # 2026-05-09 incident: hash_momentum spammed 30 SOL trades in 4h.
        self._last_closed_bar_ts: object | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        risk = entry_price * (self.stop_loss_pct / 100)
        if side == "long":
            self._in_long = True
            self._sl = entry_price - risk
            self._tp = entry_price + risk * self.risk_reward
        else:
            self._in_short = True
            self._sl = entry_price + risk
            self._tp = entry_price - risk * self.risk_reward
        self._entry = entry_price

    def export_state(self) -> dict | None:
        if not (self._in_long or self._in_short):
            return None
        return {
            "in_long": self._in_long,
            "in_short": self._in_short,
            "entry": self._entry,
            "sl": self._sl,
            "tp": self._tp,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_long = bool(state.get("in_long", side == "long"))
        self._in_short = bool(state.get("in_short", side == "short"))
        self._entry = state.get("entry", entry_price)
        self._sl = state.get("sl")
        self._tp = state.get("tp")

    def reset_state(self) -> None:
        self._in_long = False
        self._in_short = False
        self._entry = None
        self._sl = None
        self._tp = None
        self._bars_since_close = 999
        self._last_closed_bar_ts = None

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        warmup = self.mom_length * 3 + 20
        if len(candles) < warmup:
            return None

        df = candles.copy()
        df["mom0"] = df["close"] - df["close"].shift(self.mom_length)
        df["mom1"] = df["mom0"] - df["mom0"].shift(1)
        df["mom_stdev"] = df["mom0"].rolling(self.mom_length * 3).std()
        df["mom_norm"] = df.apply(
            lambda r: r["mom0"] / r["mom_stdev"] if r["mom_stdev"] > 0 else 0.0,
            axis=1,
        )
        atr_series = pta.atr(df["high"], df["low"], df["close"], length=14)
        df["atr"] = atr_series
        df["ema"] = pta.ema(df["close"], length=self.ema_length)

        closed = df.iloc[:-1]
        latest = closed.iloc[-1]
        # Live (in-progress) bar — its high/low/close update tick-by-tick
        # while the bar is forming. Use this for SL/TP exit checks so we
        # don't fire on a 4h-stale low that's been bouncing around all
        # day. The closed bar is still used for indicator inputs (mom,
        # atr, ema) which require complete data.
        live = df.iloc[-1]

        for col in ("mom0", "mom1", "mom_stdev", "mom_norm", "atr", "ema"):
            if pd.isna(latest[col]):
                return None

        close = float(latest["close"])
        prev_close = float(closed.iloc[-2]["close"])
        mom0 = float(latest["mom0"])
        mom1 = float(latest["mom1"])
        mom_norm = float(latest["mom_norm"])
        atr = float(latest["atr"])
        ema = float(latest["ema"])
        live_high = float(live["high"])
        live_low = float(live["low"])

        threshold = atr * self.mom_threshold_atr_mult

        # ---- Manage open positions (use LIVE bar for SL/TP) ----
        if self._in_long and self._sl is not None and self._tp is not None:
            if live_low <= self._sl:
                self._in_long = False
                self._bars_since_close = 0
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: low ${live_low:,.2f} <= SL ${self._sl:,.2f}",
                )
            if live_high >= self._tp:
                self._in_long = False
                self._bars_since_close = 0
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"TP hit: high ${live_high:,.2f} >= TP ${self._tp:,.2f}",
                )

        if self._in_short and self._sl is not None and self._tp is not None:
            if live_high >= self._sl:
                self._in_short = False
                self._bars_since_close = 0
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"SL hit: high ${live_high:,.2f} >= SL ${self._sl:,.2f}",
                )
            if live_low <= self._tp:
                self._in_short = False
                self._bars_since_close = 0
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=f"TP hit: low ${live_low:,.2f} <= TP ${self._tp:,.2f}",
                )

        # Cooldown advances per BAR, not per tick. Bot polls every 60s
        # but strategy is on 4h candles — without this guard, the 6-bar
        # cooldown expires in 6 minutes and re-entry storms back on the
        # same stale bar data. Track the closed bar's timestamp; only
        # bump the counter when a new bar has actually closed.
        latest_bar_ts = latest["timestamp"] if "timestamp" in latest.index else None
        if latest_bar_ts is not None and latest_bar_ts != self._last_closed_bar_ts:
            self._bars_since_close += 1
            self._last_closed_bar_ts = latest_bar_ts
        in_cooldown = self._bars_since_close < self.cooldown_bars
        flat = not self._in_long and not self._in_short

        # ---- Entry signals ----
        long_mom = (
            mom0 > threshold
            and mom1 > 0
            and mom_norm > 0.5
            and close > prev_close
            and close > ema
        )
        short_mom = (
            mom0 < -threshold
            and mom1 < 0
            and mom_norm < -0.5
            and close < prev_close
            and close < ema
        )

        # Pine: exit on opposite signal (strategy.close on reverse).
        # Engine's flip-detect would reverse on a fresh OPEN signal anyway,
        # but Pine triggers a pure close when the OPPOSITE signal fires
        # without a fresh same-side signal — emit explicit close here.
        if self._in_long and short_mom:
            self._in_long = False
            self._bars_since_close = 0
            return Signal(
                action=SignalAction.CLOSE_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"Reverse-signal close: short_mom triggered while long",
            )
        if self._in_short and long_mom:
            self._in_short = False
            self._bars_since_close = 0
            return Signal(
                action=SignalAction.CLOSE_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=f"Reverse-signal close: long_mom triggered while short",
            )

        if flat and not in_cooldown and long_mom:
            risk = close * (self.stop_loss_pct / 100)
            self._entry = close
            self._sl = close - risk
            self._tp = close + risk * self.risk_reward
            self._in_long = True
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Long: mom={mom0:.2f}>{threshold:.2f}, norm={mom_norm:.2f}, "
                    f"EMA={ema:,.2f}. SL=${self._sl:,.2f} TP=${self._tp:,.2f}"
                ),
            )

        if flat and not in_cooldown and short_mom:
            risk = close * (self.stop_loss_pct / 100)
            self._entry = close
            self._sl = close + risk
            self._tp = close - risk * self.risk_reward
            self._in_short = True
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Short: mom={mom0:.2f}<{-threshold:.2f}, norm={mom_norm:.2f}, "
                    f"EMA={ema:,.2f}. SL=${self._sl:,.2f} TP=${self._tp:,.2f}"
                ),
            )

        return None
