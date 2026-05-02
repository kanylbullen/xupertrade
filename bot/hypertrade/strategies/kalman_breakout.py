"""Kinetic Kalman Breakout — Pine v6 port.

Two-state Kalman filter (position + velocity) on close prices produces a
smoothed estimate of "fair value". Bands are mean ± multiplier × MAE,
where MAE = SMA of |close - kalmanPrice| over `band_lookback` bars.
Long on close crossover upper band, short on close crossunder lower band.
Strategy reverses on opposite signal — engine flip-detect handles the
close-then-open. Long & short, no fixed SL/TP.

Source logic (verified byte-equivalent):
    State: x_p (position estimate), x_v (velocity estimate),
           covariance matrix [[p00, p01],[p10, p11]]

    PREDICT (transition matrix [[1,1],[0,1]]):
        pPrime = x_p + x_v
        vPrime = x_v
        a00=p00+p10, a01=p01+p11, a10=p10, a11=p11
        p00'=a00+a01, p01'=a01, p10'=a10+a11, p11'=a11
        p00' += processNoisePos
        p11' += processNoiseVel

    UPDATE (measurement = close, H = [1, 0]):
        y = close - pPrime
        S = p00' + measurementNoise
        K0 = p00'/S, K1 = p10'/S
        x_p = pPrime + K0 * y
        x_v = vPrime + K1 * y
        I-KH = [[1-K0, 0], [-K1, 1]]
        new covariance = (I-KH) @ p_pred

    BANDS:
        absDiff = |close - x_p|
        mae = SMA(absDiff, band_lookback)
        upper = x_p + bandMultiplier * mae
        lower = x_p - bandMultiplier * mae

    SIGNALS:
        bull = ta.crossover(close, upper)   → close[-2] <= upper[-2] AND close[-1] > upper[-1]
        bear = ta.crossunder(close, lower)  → close[-2] >= lower[-2] AND close[-1] < lower[-1]

Implementation note: Kalman state is recomputed from scratch each on_candle
to avoid state drift across restarts / out-of-order ticks. O(n) per tick is
acceptable for 200+ bar histories. Initial state at bar 0:
    x_p=close[0], x_v=0, p00=1, p01=0, p10=0, p11=1.

Optimized for ETH (per source comment).
"""

import numpy as np
import pandas as pd

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class KalmanBreakoutStrategy(Strategy):
    name = "kalman_breakout"
    symbol = "ETH"
    timeframe = "1h"
    leverage = 1

    process_noise_pos: float = 0.05
    process_noise_vel: float = 0.0001
    measurement_noise: float = 250.0
    band_lookback: int = 200
    band_multiplier: float = 2.6

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_long: bool = False
        self._in_short: bool = False

    def restore_state(self, side: str, entry_price: float) -> None:
        self._in_long = side == "long"
        self._in_short = side == "short"

    def export_state(self) -> dict | None:
        if not (self._in_long or self._in_short):
            return None
        return {"in_long": self._in_long, "in_short": self._in_short}

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_long = bool(state.get("in_long", side == "long"))
        self._in_short = bool(state.get("in_short", side == "short"))

    def _kalman_series(self, closes: np.ndarray) -> np.ndarray:
        """Run the 2-state Kalman filter over closes; return per-bar x_p estimate."""
        n = len(closes)
        out = np.empty(n, dtype=float)

        # Init at bar 0
        x_p = float(closes[0])
        x_v = 0.0
        p00, p01, p10, p11 = 1.0, 0.0, 0.0, 1.0
        out[0] = x_p

        q_p = self.process_noise_pos
        q_v = self.process_noise_vel
        r = self.measurement_noise

        for i in range(1, n):
            # PREDICT
            p_prime = x_p + x_v
            v_prime = x_v
            a00 = p00 + p10
            a01 = p01 + p11
            a10 = p10
            a11 = p11
            p00_ = a00 + a01
            p01_ = a01
            p10_ = a10 + a11
            p11_ = a11
            p00_ += q_p
            p11_ += q_v

            # UPDATE
            z = float(closes[i])
            y = z - p_prime
            s = p00_ + r
            k0 = p00_ / s
            k1 = p10_ / s
            x_p = p_prime + k0 * y
            x_v = v_prime + k1 * y
            # I-KH = [[1-k0, 0], [-k1, 1]]
            i00 = 1.0 - k0
            i10 = -k1
            pp00 = i00 * p00_
            pp01 = i00 * p01_
            pp10 = i10 * p00_ + p10_
            pp11 = i10 * p01_ + p11_
            p00, p01, p10, p11 = pp00, pp01, pp10, pp11

            out[i] = x_p

        return out

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # Need band_lookback bars for the SMA + 2 closed bars for crossover
        if len(candles) < self.band_lookback + 5:
            return None

        df = candles
        closed = df.iloc[:-1]
        if len(closed) < self.band_lookback + 2:
            return None

        closes = closed["close"].to_numpy(dtype=float)
        kalman = self._kalman_series(closes)

        abs_diff = np.abs(closes - kalman)
        # SMA over band_lookback — only need last two values
        # Use pandas for clarity & NaN handling consistent with pta.sma
        mae_series = pd.Series(abs_diff).rolling(self.band_lookback).mean().to_numpy()

        upper = kalman + self.band_multiplier * mae_series
        lower = kalman - self.band_multiplier * mae_series

        # Last two closed bars
        c_cur = float(closes[-1])
        c_prev = float(closes[-2])
        u_cur = float(upper[-1])
        u_prev = float(upper[-2])
        l_cur = float(lower[-1])
        l_prev = float(lower[-2])

        if any(np.isnan(v) for v in (u_cur, u_prev, l_cur, l_prev)):
            return None

        bull_signal = c_prev <= u_prev and c_cur > u_cur
        bear_signal = c_prev >= l_prev and c_cur < l_cur

        kp = float(kalman[-1])

        # Bull: open long (engine flips short→long if needed)
        if bull_signal and not self._in_long:
            self._in_long = True
            self._in_short = False
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Kalman bull breakout: close ${c_cur:,.2f} > upper ${u_cur:,.2f} "
                    f"(kalman ${kp:,.2f})"
                ),
            )

        # Bear: open short
        if bear_signal and not self._in_short:
            self._in_short = True
            self._in_long = False
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"Kalman bear breakout: close ${c_cur:,.2f} < lower ${l_cur:,.2f} "
                    f"(kalman ${kp:,.2f})"
                ),
            )

        return None
