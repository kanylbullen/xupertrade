"""VVV defensive hedge strategy.

NOT a Pine port. Custom strategy designed for the specific use case of
hedging a long-term VVV holding (staked for DIEM) against trend reversal.

Mainnet-only: the hedge only makes sense against real staked VVV. On
paper/testnet there's nothing to hedge, so `on_candle` returns `None`
immediately to avoid pointless signals (and safety-cap rejections when
the holding_vvv × price notional exceeds the paper/testnet caps).

Goal: protect spot value when the long-term uptrend breaks. Open a short
of size = `holding_vvv` (NOT engine's notional sizing) when 3 of 4
regime-shift indicators turn bearish on 4h. Close the short when 2 of 4
re-confirm bullish. Hard SL 10% above entry as last-resort liquidation
guard (with 2× leverage that's a 20% margin loss — survivable).

Indicator ensemble (all on closed 4h candles):
    1. EMA21 vs EMA55: bearish if EMA21 < EMA55 AND close < EMA55.
       Long-term trend is broken.
    2. ATR(14) chandelier exit: bearish if close < (highest high in last
       30 bars - ATR × 3.0). Trend-reversal trigger from the recent high.
    3. RSI(14) bearish divergence: bearish if price made a higher high in
       the last 20 bars vs the prior 20 bars, BUT RSI made a lower high
       over the same windows. Momentum dying while price still climbs.
    4. Volume distribution: bearish if sum(volume, 7d) > 1.5 × avg(7d
       volume window over the last 30 days). Strong selling absorbed.

Vote counts:
    short_signal:  bearish_score >= 3 of 4 AND not currently short
    close_signal:  bearish_score <= 1 of 4 AND currently short

Hard SL: 10% above entry. Hits → close short, log warning.

Notes for live use:
- Default holding_vvv = 400. Adjust the class attr or runtime-override
  via dashboard /strategies leverage input (which is misnamed for this
  case — could add a separate param input later).
- The strategy emits Signal(size=holding_vvv), bypassing the engine's
  MAX_POSITION_SIZE_USD × leverage formula. Leverage class attr (2)
  is still pushed to HL so the actual margin requirement matches.
- Max-total-exposure cap in runner may block this if other positions
  already fill the budget — set MAX_TOTAL_EXPOSURE_USD high enough
  to fit the hedge notional (400 VVV × current price × 2× leverage).
"""

from __future__ import annotations

import logging

import pandas as pd
import pandas_ta as pta

from hypertrade.config import settings
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register

logger = logging.getLogger(__name__)


@register
class VVVHedgeStrategy(Strategy):
    name = "vvv_hedge"
    symbol = "VVV"
    timeframe = "4h"
    leverage = 2  # conservative; pushed to HL on bot startup

    # Holding to hedge — emitted as exact short size, not derived from
    # MAX_POSITION_SIZE_USD. Adjust here if your stake changes.
    holding_vvv: float = 400.0

    # Indicator parameters
    ema_fast_len: int = 21
    ema_slow_len: int = 55
    atr_len: int = 14
    chandelier_lookback: int = 30  # bars for highest high
    chandelier_atr_mult: float = 3.0
    rsi_len: int = 14
    rsi_div_window: int = 20  # bars per half-window for divergence
    vol_recent_days: int = 7
    vol_baseline_days: int = 30
    vol_ratio_threshold: float = 1.5

    # EMA as MANDATORY filter (not a vote). Backtest on first 6 months of
    # VVV uptrend showed 3 of 5 false signals all had EMA NOT bearish; the
    # one big winner (+$620 on a 31% pullback) had EMA bearish. So EMA
    # bearish is now required for entry. Set False to revert to pure 4-vote.
    require_ema_bearish: bool = True

    # Vote thresholds (now over the OTHER 3 indicators when EMA is required)
    bearish_votes_to_open: int = 2   # of remaining 3 (chandelier/rsi_div/volume)
    bearish_votes_to_keep: int = 1   # if drops below this → close short

    # Risk
    hard_sl_pct: float = 0.10  # 10% above short entry

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_short: bool = False
        self._entry_price: float | None = None
        self._sl: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        if side == "short":
            self._in_short = True
            self._entry_price = entry_price
            self._sl = entry_price * (1 + self.hard_sl_pct)
        else:
            # Strategy is short-only — long restore shouldn't happen,
            # treat defensively
            self._in_short = False
            self._entry_price = None
            self._sl = None

    def export_state(self) -> dict | None:
        if not self._in_short:
            return None
        return {
            "in_short": self._in_short,
            "entry_price": self._entry_price,
            "sl": self._sl,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        self._in_short = bool(state.get("in_short", side == "short"))
        self._entry_price = state.get("entry_price", entry_price)
        self._sl = state.get("sl")

    def reset_state(self) -> None:
        self._in_short = False
        self._entry_price = None
        self._sl = None

    def _bars_per_day(self) -> int:
        """Approximate bars per day for the active timeframe."""
        tf = self.timeframe.lower()
        if tf.endswith("h"):
            return max(1, 24 // int(tf[:-1] or 1))
        if tf.endswith("d"):
            return 1
        if tf.endswith("m"):
            return max(1, (24 * 60) // int(tf[:-1] or 1))
        return 6  # default: assume 4h

    def _vote_bearish(self, df: pd.DataFrame) -> tuple[int, dict]:
        """Run the 4 indicators on the latest closed bar; return (count, breakdown)."""
        breakdown: dict[str, bool] = {}
        closed = df.iloc[:-1]  # use last CLOSED bar
        latest = closed.iloc[-1]
        close = float(latest["close"])

        # 1. EMA21 vs EMA55
        ema_fast = pta.ema(closed["close"], length=self.ema_fast_len)
        ema_slow = pta.ema(closed["close"], length=self.ema_slow_len)
        if (
            ema_fast is not None and ema_slow is not None
            and not pd.isna(ema_fast.iloc[-1]) and not pd.isna(ema_slow.iloc[-1])
        ):
            breakdown["ema"] = (
                float(ema_fast.iloc[-1]) < float(ema_slow.iloc[-1])
                and close < float(ema_slow.iloc[-1])
            )

        # 2. Chandelier exit
        atr = pta.atr(
            closed["high"], closed["low"], closed["close"], length=self.atr_len
        )
        if atr is not None and not pd.isna(atr.iloc[-1]):
            window = closed.iloc[-self.chandelier_lookback:]
            highest = float(window["high"].max())
            chandelier = highest - float(atr.iloc[-1]) * self.chandelier_atr_mult
            breakdown["chandelier"] = close < chandelier

        # 3. RSI bearish divergence
        rsi = pta.rsi(closed["close"], length=self.rsi_len)
        if rsi is not None and len(rsi) >= 2 * self.rsi_div_window:
            prior = closed.iloc[-2 * self.rsi_div_window: -self.rsi_div_window]
            recent = closed.iloc[-self.rsi_div_window:]
            prior_rsi = rsi.iloc[-2 * self.rsi_div_window: -self.rsi_div_window]
            recent_rsi = rsi.iloc[-self.rsi_div_window:]
            if (
                len(prior) > 0 and len(recent) > 0
                and not prior_rsi.isna().all() and not recent_rsi.isna().all()
            ):
                price_higher_high = float(recent["high"].max()) > float(prior["high"].max())
                rsi_lower_high = float(recent_rsi.max()) < float(prior_rsi.max())
                breakdown["rsi_div"] = price_higher_high and rsi_lower_high

        # 4. Volume distribution
        bpd = self._bars_per_day()
        recent_bars = self.vol_recent_days * bpd
        baseline_bars = self.vol_baseline_days * bpd
        if len(closed) >= baseline_bars:
            recent_vol = float(closed["volume"].iloc[-recent_bars:].sum())
            baseline_avg_window = float(
                closed["volume"].iloc[-baseline_bars:].rolling(recent_bars).sum().mean()
            )
            if baseline_avg_window > 0:
                ratio = recent_vol / baseline_avg_window
                breakdown["volume"] = ratio > self.vol_ratio_threshold

        bearish_count = sum(1 for v in breakdown.values() if v)
        return bearish_count, breakdown

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # Hedges real staked VVV; only mainnet has actual holdings to hedge
        if settings.exchange_mode != "mainnet":
            return None
        # Need enough history for the longest indicator
        warmup = max(
            self.ema_slow_len,
            self.atr_len + self.chandelier_lookback,
            2 * self.rsi_div_window + self.rsi_len,
            self.vol_baseline_days * self._bars_per_day(),
        ) + 5
        if len(candles) < warmup:
            return None

        latest = candles.iloc[-1]
        high = float(latest["high"])
        close = float(latest["close"])

        # ----- Manage open short first -----
        if self._in_short and self._sl is not None:
            if high >= self._sl:
                self._in_short = False
                entry = self._entry_price or 0
                self._entry_price = None
                self._sl = None
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    size=self.holding_vvv,
                    reason=f"HARD SL: high ${high:.4f} >= ${self._sl or 0:.4f} (entry ${entry:.4f}). Hedge unwound — manual review recommended.",
                )

            # Check if regime has flipped back to bullish (close hedge).
            # When EMA filter is required, also close if EMA itself flips
            # back to bullish — that's the strongest single signal.
            bearish_count, breakdown = self._vote_bearish(candles)
            if self.require_ema_bearish and not breakdown.get("ema", False):
                self._in_short = False
                self._entry_price = None
                self._sl = None
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    size=self.holding_vvv,
                    reason=f"EMA filter flipped bullish ({breakdown}). Closing hedge.",
                )
            if bearish_count <= self.bearish_votes_to_keep - 1:
                self._in_short = False
                self._entry_price = None
                self._sl = None
                return Signal(
                    action=SignalAction.CLOSE_SHORT,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    size=self.holding_vvv,
                    reason=(
                        f"Regime flipped bullish: only {bearish_count}/4 bearish "
                        f"({breakdown}). Closing hedge."
                    ),
                )
            return None

        # ----- Flat: check for short signal -----
        bearish_count, breakdown = self._vote_bearish(candles)

        # MANDATORY EMA filter (when enabled): EMA must be bearish to even
        # consider opening. Backtest showed this filter eliminates 3 of 4
        # false signals during a sustained uptrend.
        if self.require_ema_bearish and not breakdown.get("ema", False):
            return None

        # When EMA is required, count only the OTHER 3 indicators against
        # bearish_votes_to_open (default 2 of 3).
        if self.require_ema_bearish:
            non_ema_bearish = sum(
                1 for k, v in breakdown.items() if k != "ema" and v
            )
            should_open = non_ema_bearish >= self.bearish_votes_to_open
        else:
            should_open = bearish_count >= self.bearish_votes_to_open

        if should_open:
            self._in_short = True
            self._entry_price = close
            self._sl = close * (1 + self.hard_sl_pct)
            return Signal(
                action=SignalAction.OPEN_SHORT,
                symbol=self.symbol,
                strategy_name=self.name,
                size=self.holding_vvv,
                reason=(
                    f"REGIME SHORT: {bearish_count}/4 bearish ({breakdown}). "
                    f"Hedging {self.holding_vvv} VVV @ ${close:.4f}, "
                    f"hard SL ${self._sl:.4f} (+{self.hard_sl_pct*100:.0f}%)."
                ),
            )

        return None
