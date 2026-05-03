"""Oleg Aryukov Multi-Indicator Ensemble — Pine v6 port.

Multi-indicator voting strategy. Each enabled indicator casts a buy/sell vote
on every bar; long entry fires when buy_signals >= min_confirmations and trend
filter agrees. Short entry symmetrically. Uses fixed % stop-loss / take-profit.
Optional trailing stop.

Indicators that vote (votes-per-indicator in parens):
    1. RSI         — buy if RSI < 30, sell if RSI > 70                      (1)
    2. Williams %R — two periods (6 & 12), each votes < -80 / > -20         (2)
    3. TSI         — divergence: |tsi - signal| > 15 on the appropriate side(1)
    4. KDJ         — J<20 + |K-D|>20 buy ; J>80 + |K-D|>20 sell             (1)
    5. %BB         — buy < 0, sell > 1                                      (1)
    6. Nadaraya-Watson — buy if close < NW_lower band, sell if > NW_upper   (1)
    7. RCI ribbon  — all 3 (fast/med/slow) below -80 buy / above +80 sell   (1)

Trend filter (EMA50 vs EMA200) restricts entries to with-trend by default.

Entry: votes_buy >= min_confirmations (default 3) AND trend filter AND flat.
Exit:  fixed % SL/TP from entry. Optional trailing stop in % terms.

Russian comments in the original were only labels and explanations; full
translation in this docstring.

Notes / deviations:
    - **Nadaraya-Watson** is implemented exactly per Pine: Gaussian-kernel
      weighted average over the last `nw_lookback` bars. Same band formula
      (estimate * (1 ± nw_bandwidth/100)).
    - **RCI** uses Pearson correlation of `percentrank(close)` vs.
      `percentrank(bar_index)` — Pine's `ta.correlation(rank_price, rank_time)`
      semantics.
    - **TSI** uses double-EMA-smoothed price-change.
    - Trailing stop is implemented as percentage-distance trail from highest
      high (long) / lowest low (short) since entry.
    - The Pine version uses `min_confirmations=3` default; we keep that.
    - We DO NOT model fee/commission; the engine handles fees out of band.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pandas_ta as pta

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


def _nadaraya_watson(closes: np.ndarray, bandwidth: float, lookback: int) -> float:
    """Gaussian-kernel weighted average of last `lookback` closes.

    Matches Pine: weight_i = exp(-i^2 / (2*bandwidth^2)), i in [0..lookback-1],
    closes[0] is the most recent bar.
    """
    n = min(lookback, len(closes))
    if n <= 0:
        return float("nan")
    window = closes[-n:][::-1]  # reverse so index 0 = most recent
    idx = np.arange(n)
    weights = np.exp(-(idx ** 2) / (2.0 * bandwidth ** 2))
    sw = float(weights.sum())
    if sw <= 0:
        return float(closes[-1])
    return float((window * weights).sum() / sw)


def _rci(close: pd.Series, length: int) -> pd.Series:
    """RCI ≈ Pearson correlation of percentrank(close) vs. percentrank(time),
    as per Pine `ta.correlation(rank_price, rank_time, length)` * 100.
    """
    if length < 2 or len(close) < length:
        return pd.Series([float("nan")] * len(close), index=close.index)

    out = np.full(len(close), np.nan)
    closes = close.values
    for i in range(length - 1, len(close)):
        window = closes[i - length + 1 : i + 1]
        # percentrank within window — relative ordinal rank in [0..1]
        order = np.argsort(window)
        ranks = np.empty_like(order, dtype=float)
        ranks[order] = np.arange(length, dtype=float) / max(length - 1, 1)
        # time rank is just linear 0..1
        time_rank = np.arange(length, dtype=float) / max(length - 1, 1)
        if np.std(ranks) < 1e-12 or np.std(time_rank) < 1e-12:
            out[i] = 0.0
            continue
        corr = float(np.corrcoef(ranks, time_rank)[0, 1])
        out[i] = corr * 100.0
    return pd.Series(out, index=close.index)


@register
class OlegAryukovStrategy(Strategy):
    name = "oleg_aryukov"
    symbol = "ETH"
    timeframe = "1h"
    leverage = 1

    # Indicator activation
    use_rsi: bool = True
    use_williams: bool = True
    use_tsi: bool = True
    use_kdj: bool = True
    use_bb_percent: bool = True
    use_nadaraya: bool = True
    use_rci: bool = True

    # RSI
    rsi_length: int = 12
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Williams %R
    williams_length_6: int = 6
    williams_length_12: int = 12
    williams_oversold: float = -80.0
    williams_overbought: float = -20.0

    # TSI
    tsi_long: int = 25
    tsi_short: int = 13
    tsi_signal: int = 13
    tsi_divergence: float = 15.0

    # KDJ
    kdj_length: int = 9
    kdj_signal_k: int = 3
    kdj_signal_d: int = 3
    kdj_divergence: float = 20.0

    # Bollinger
    bb_length: int = 20
    bb_mult: float = 2.0

    # Nadaraya-Watson
    nw_bandwidth: float = 3.0
    nw_lookback: int = 50

    # RCI
    rci_fast: int = 9
    rci_medium: int = 26
    rci_slow: int = 52
    rci_oversold: float = -80.0
    rci_overbought: float = 80.0

    # Risk
    stop_loss_percent: float = 2.0
    take_profit_percent: float = 4.0
    use_trailing: bool = True
    trailing_percent: float = 1.0

    # Filters
    min_confirmations: int = 3
    check_trend: bool = True

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._position_side: str | None = None
        self._entry_price: float | None = None
        self._stop_loss: float | None = None
        self._take_profit: float | None = None
        # trail extreme: highest high since entry (long) / lowest low (short)
        self._trail_extreme: float | None = None

    # ----- state lifecycle --------------------------------------------------
    def restore_state(self, side: str, entry_price: float) -> None:
        self._position_side = side
        self._entry_price = entry_price
        sl_pct = self.stop_loss_percent / 100.0
        tp_pct = self.take_profit_percent / 100.0
        if side == "long":
            self._stop_loss = entry_price * (1 - sl_pct)
            self._take_profit = entry_price * (1 + tp_pct)
        else:
            self._stop_loss = entry_price * (1 + sl_pct)
            self._take_profit = entry_price * (1 - tp_pct)
        self._trail_extreme = entry_price

    def export_state(self) -> dict | None:
        if self._position_side is None:
            return None
        return {
            "position_side": self._position_side,
            "entry_price": self._entry_price,
            "stop_loss": self._stop_loss,
            "take_profit": self._take_profit,
            "trail_extreme": self._trail_extreme,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._position_side = state.get("position_side", side)
        self._entry_price = state.get("entry_price", entry_price)
        self._stop_loss = state.get("stop_loss")
        self._take_profit = state.get("take_profit")
        self._trail_extreme = state.get("trail_extreme", entry_price)
        if self._stop_loss is None or self._take_profit is None:
            self.restore_state(side, entry_price)

    def _reset(self) -> None:
        self._position_side = None
        self._entry_price = None
        self._stop_loss = None
        self._take_profit = None
        self._trail_extreme = None

    def reset_state(self) -> None:
        self._reset()

    # ----- main loop --------------------------------------------------------
    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        warmup = max(
            self.bb_length, self.tsi_long * 2, self.rci_slow,
            self.nw_lookback, 200,  # for ema_slow
        ) + 20
        if len(candles) < warmup:
            return None

        df = candles.copy()
        latest = df.iloc[-1]
        high = float(latest["high"])
        low = float(latest["low"])
        close = float(latest["close"])

        # ----- manage open position first -----
        if self._position_side is not None and self._entry_price is not None:
            if self.use_trailing and self._trail_extreme is not None:
                trail_dist = self._entry_price * self.trailing_percent / 100.0
                if self._position_side == "long":
                    if high > self._trail_extreme:
                        self._trail_extreme = high
                    trail_stop = self._trail_extreme - trail_dist
                    if self._stop_loss is None or trail_stop > self._stop_loss:
                        self._stop_loss = trail_stop
                else:  # short
                    if low < self._trail_extreme:
                        self._trail_extreme = low
                    trail_stop = self._trail_extreme + trail_dist
                    if self._stop_loss is None or trail_stop < self._stop_loss:
                        self._stop_loss = trail_stop

            sl = self._stop_loss
            tp = self._take_profit
            if self._position_side == "long":
                if sl is not None and low <= sl:
                    entry = self._entry_price
                    self._reset()
                    return Signal(
                        action=SignalAction.CLOSE_LONG, symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"SL hit at ${sl:,.2f} (entry ${entry:,.2f}, low ${low:,.2f})",
                    )
                if tp is not None and high >= tp:
                    entry = self._entry_price
                    self._reset()
                    return Signal(
                        action=SignalAction.CLOSE_LONG, symbol=self.symbol,
                        strategy_name=self.name, price=tp,
                        reason=f"TP filled at ${tp:,.2f} (entry ${entry:,.2f}, high ${high:,.2f})",
                    )
            else:
                if sl is not None and high >= sl:
                    entry = self._entry_price
                    self._reset()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT, symbol=self.symbol,
                        strategy_name=self.name,
                        reason=f"SL hit at ${sl:,.2f} (entry ${entry:,.2f}, high ${high:,.2f})",
                    )
                if tp is not None and low <= tp:
                    entry = self._entry_price
                    self._reset()
                    return Signal(
                        action=SignalAction.CLOSE_SHORT, symbol=self.symbol,
                        strategy_name=self.name, price=tp,
                        reason=f"TP filled at ${tp:,.2f} (entry ${entry:,.2f}, low ${low:,.2f})",
                    )
            return None

        # ----- Flat: compute indicators and tally votes -----
        buy = 0
        sell = 0

        # RSI
        if self.use_rsi:
            rsi_series = pta.rsi(df["close"], length=self.rsi_length)
            if rsi_series is not None and not pd.isna(rsi_series.iloc[-1]):
                rsi = float(rsi_series.iloc[-1])
                if rsi < self.rsi_oversold:
                    buy += 1
                if rsi > self.rsi_overbought:
                    sell += 1

        # Williams %R (two periods)
        if self.use_williams:
            for length in (self.williams_length_6, self.williams_length_12):
                wpr = pta.willr(
                    df["high"], df["low"], df["close"], length=length
                )
                if wpr is not None and not pd.isna(wpr.iloc[-1]):
                    val = float(wpr.iloc[-1])
                    if val < self.williams_oversold:
                        buy += 1
                    if val > self.williams_overbought:
                        sell += 1

        # TSI
        if self.use_tsi:
            pc = df["close"].diff()
            ds_pc = pta.ema(pta.ema(pc, length=self.tsi_long), length=self.tsi_short)
            ds_abs = pta.ema(
                pta.ema(pc.abs(), length=self.tsi_long), length=self.tsi_short
            )
            denom = float(ds_abs.iloc[-1]) if ds_abs is not None else 0.0
            tsi_val = (
                100.0 * float(ds_pc.iloc[-1]) / denom if denom and not pd.isna(denom) else 0.0
            )
            tsi_signal_series = pta.ema(
                pd.Series(
                    [
                        0.0 if (ds_abs.iloc[i] == 0 or pd.isna(ds_abs.iloc[i])) else
                        100.0 * ds_pc.iloc[i] / ds_abs.iloc[i]
                        for i in range(len(df))
                    ],
                    index=df.index,
                ),
                length=self.tsi_signal,
            )
            if tsi_signal_series is not None and not pd.isna(tsi_signal_series.iloc[-1]):
                tsi_sig = float(tsi_signal_series.iloc[-1])
                divergence = abs(tsi_val - tsi_sig)
                if divergence > self.tsi_divergence and tsi_val < tsi_sig:
                    buy += 1
                if divergence > self.tsi_divergence and tsi_val > tsi_sig:
                    sell += 1

        # KDJ
        if self.use_kdj:
            kdj_high = df["high"].rolling(self.kdj_length).max()
            kdj_low = df["low"].rolling(self.kdj_length).min()
            denom = (kdj_high - kdj_low).replace(0, np.nan)
            rsv = ((df["close"] - kdj_low) / denom * 100.0).fillna(50.0)
            k = rsv.rolling(self.kdj_signal_k).mean()
            d = k.rolling(self.kdj_signal_d).mean()
            j = 3 * k - 2 * d
            if not pd.isna(k.iloc[-1]) and not pd.isna(d.iloc[-1]) and not pd.isna(j.iloc[-1]):
                k_v = float(k.iloc[-1])
                d_v = float(d.iloc[-1])
                j_v = float(j.iloc[-1])
                kd_div = abs(k_v - d_v)
                if j_v < 20 and kd_div > self.kdj_divergence:
                    buy += 1
                if j_v > 80 and kd_div > self.kdj_divergence:
                    sell += 1

        # %BB
        if self.use_bb_percent:
            bb = pta.bbands(df["close"], length=self.bb_length, std=self.bb_mult)
            if bb is not None and len(bb.columns) >= 3:
                # bbands columns: BBL_, BBM_, BBU_, BBB_, BBP_
                lower = bb.iloc[-1, 0]
                upper = bb.iloc[-1, 2]
                den = upper - lower
                bb_pct = (close - lower) / den if den != 0 and not pd.isna(den) else 0.5
                if bb_pct < 0:
                    buy += 1
                if bb_pct > 1:
                    sell += 1

        # Nadaraya-Watson
        if self.use_nadaraya:
            nw_est = _nadaraya_watson(
                df["close"].values, self.nw_bandwidth, self.nw_lookback
            )
            if not math.isnan(nw_est):
                nw_upper = nw_est * (1 + self.nw_bandwidth / 100.0)
                nw_lower = nw_est * (1 - self.nw_bandwidth / 100.0)
                if close < nw_lower:
                    buy += 1
                if close > nw_upper:
                    sell += 1

        # RCI ribbon (always evaluated in Pine — no toggle, mirror that)
        if self.use_rci:
            rci_f = _rci(df["close"], self.rci_fast).iloc[-1]
            rci_m = _rci(df["close"], self.rci_medium).iloc[-1]
            rci_s = _rci(df["close"], self.rci_slow).iloc[-1]
            if not (pd.isna(rci_f) or pd.isna(rci_m) or pd.isna(rci_s)):
                if (
                    rci_f < self.rci_oversold and rci_m < self.rci_oversold
                    and rci_s < self.rci_oversold
                ):
                    buy += 1
                if (
                    rci_f > self.rci_overbought and rci_m > self.rci_overbought
                    and rci_s > self.rci_overbought
                ):
                    sell += 1

        # Trend filter
        ema_fast = pta.ema(df["close"], length=50).iloc[-1]
        ema_slow = pta.ema(df["close"], length=200).iloc[-1]
        if pd.isna(ema_fast) or pd.isna(ema_slow):
            return None
        trend_up = ema_fast > ema_slow
        trend_down = ema_fast < ema_slow
        trend_ok_long = (trend_up or not trend_down) if self.check_trend else True
        trend_ok_short = (trend_down or not trend_up) if self.check_trend else True

        long_cond = buy >= self.min_confirmations and trend_ok_long
        short_cond = sell >= self.min_confirmations and trend_ok_short

        if long_cond:
            sl = close * (1 - self.stop_loss_percent / 100.0)
            tp = close * (1 + self.take_profit_percent / 100.0)
            self._position_side = "long"
            self._entry_price = close
            self._stop_loss = sl
            self._take_profit = tp
            self._trail_extreme = close
            return Signal(
                action=SignalAction.OPEN_LONG, symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=sl, take_profit=tp,
                reason=(
                    f"Long ensemble: {buy}/{self.min_confirmations}+ buy votes, "
                    f"sells {sell}, trend {'up' if trend_up else 'flat'}. "
                    f"SL=${sl:,.2f} TP=${tp:,.2f}"
                ),
            )
        if short_cond:
            sl = close * (1 + self.stop_loss_percent / 100.0)
            tp = close * (1 - self.take_profit_percent / 100.0)
            self._position_side = "short"
            self._entry_price = close
            self._stop_loss = sl
            self._take_profit = tp
            self._trail_extreme = close
            return Signal(
                action=SignalAction.OPEN_SHORT, symbol=self.symbol,
                strategy_name=self.name,
                stop_loss=sl, take_profit=tp,
                reason=(
                    f"Short ensemble: {sell}/{self.min_confirmations}+ sell votes, "
                    f"buys {buy}, trend {'down' if trend_down else 'flat'}. "
                    f"SL=${sl:,.2f} TP=${tp:,.2f}"
                ),
            )
        return None
