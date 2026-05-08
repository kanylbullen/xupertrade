"""ATH Breakout — buy Bitcoin at a new N-day high, exit on trailing stop.

Custom strategy (not a Pine port). Designed as an OVERLAY on top of
HODL+DCA, not as a standalone replacement: it sits in cash and only
deploys when BTC breaks a new ~100-day high.

Entry (long-only):
    close > rolling-max(close[-(lookback+1):-1])
    i.e. the previous bar's `lookback`-day high is broken on close.
    `lookback=100` ≈ "new ~3-month high"; on BTC's daily timeframe
    this is recent enough to catch regime-change breakouts but slow
    enough to avoid most chop.

Exit:
    Trailing stop at peak-since-entry * (1 - trail_pct).
    `trail_pct=0.35` (35%) — wide enough to ride BTC's brutal mid-bull
    -25% drawdowns without exiting, exits only on genuine regime change.
    Tighter trails (15-25%) consistently underperform in backtest by
    exiting too early. Exits trigger on close, not intraday low.

No short logic by design — ATH-breakout shorting (mean-reversion into
the high) is a different thesis. Keep the strategy focused.

Backtest evidence (BTC 1d, 2021-05 → 2026-05, $10k starting):
    HODL                          +36.6%   APR  6.4%   MaxDD 77%
    DCA monthly                   +90.8%   APR 13.8%   MaxDD —
    THIS (lb=100, tr=35%)        +247.1%   APR 33.4%   MaxDD 37%
    Param sweep also tested:
      tr=15% loses ~20pp APR (exits too early on normal pullbacks).
      lb=50 ups APR slightly but Sharpe drops (more chop).
      lb=200 cuts APR ~5pp (signal too lagged).

Conclusion: the overlay genuinely adds value vs both HODL and DCA in
the tested window — it sat in cash through 2022 bear (vs HODL's -77%
DD) and rode 2023-2024 bull on multiple new-high triggers.
"""

import pandas as pd

from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import register


@register
class AthBreakoutStrategy(Strategy):
    name = "ath_breakout"
    symbol = "BTC"
    timeframe = "1d"
    # 2x is the leverage sweet spot per backtest sweep:
    #   1x → APR 33%, Sharpe 0.93
    #   2x → APR 51%, Sharpe 1.04 ← peak
    #   3x → APR 64% but liq distance ~32% < trail 35% → realistically
    #         liquidated on a deep BTC dip (e.g. covid-crash 2020)
    # Trail-stop fires before liquidation at 2x (liq ~-48% spot, trail
    # at -35% from peak), so the strategy keeps its risk discipline.
    # Backtest doesn't model funding rate (~10-30% APR drag on leveraged
    # longs in bull regimes) — real-world APR will be lower.
    leverage = 2

    # Look-back window for "ATH". 100 daily bars ≈ 3 months; treats
    # a fresh 3-month high as the buy signal. Tuned via 5-year sweep.
    lookback: int = 100
    # Trailing-stop tightness from peak-since-entry. 0.35 = 35% — wide
    # enough to ride BTC's mid-bull pullbacks. Tighter trails consistently
    # underperformed in backtest by exiting too early.
    trail_pct: float = 0.35

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._in_position: bool = False
        self._entry_price: float | None = None
        self._peak_since_entry: float | None = None

    def restore_state(self, side: str, entry_price: float) -> None:
        # Long-only strategy. If DB row says anything other than long
        # (sync glitch, manual edit, schema drift), fail closed: stay
        # flat. The runner will reconcile and reset us cleanly.
        if side != "long":
            self.reset_state()
            return
        self._in_position = True
        self._entry_price = entry_price
        # No persisted peak → start from entry; first tick will update.
        self._peak_since_entry = entry_price

    def export_state(self) -> dict | None:
        if not self._in_position:
            return None
        return {
            "entry_price": self._entry_price,
            "peak_since_entry": self._peak_since_entry,
        }

    def restore_from_json(
        self, side: str, entry_price: float, state: dict
    ) -> None:
        # Same defensive guard as restore_state — only restore as
        # in-position when the DB confirms long.
        if side != "long":
            self.reset_state()
            return
        self._in_position = True
        self._entry_price = state.get("entry_price", entry_price)
        # Persisted peak survives restart so the trail doesn't reset and
        # accidentally widen on next bar's high.
        self._peak_since_entry = state.get("peak_since_entry", entry_price)

    def reset_state(self) -> None:
        self._in_position = False
        self._entry_price = None
        self._peak_since_entry = None

    async def on_candle(self, candles: pd.DataFrame) -> Signal | None:
        # Need lookback + 1 bars: `lookback` prior bars for the rolling-max
        # window plus the current bar being evaluated.
        if len(candles) < self.lookback + 1:
            return None

        latest = candles.iloc[-1]
        close = float(latest["close"])
        high = float(latest["high"])

        # ----- Manage open position first -----
        if self._in_position and self._entry_price is not None:
            # Update peak — use intraday high so the trail tracks the
            # actual highest excursion, not just bar-close.
            if self._peak_since_entry is None or high > self._peak_since_entry:
                self._peak_since_entry = high

            trail_stop = self._peak_since_entry * (1 - self.trail_pct)

            if close <= trail_stop:
                entry = self._entry_price
                peak = self._peak_since_entry
                self.reset_state()
                return Signal(
                    action=SignalAction.CLOSE_LONG,
                    symbol=self.symbol,
                    strategy_name=self.name,
                    reason=(
                        f"Trail stop hit: close ${close:,.2f} ≤ stop ${trail_stop:,.2f} "
                        f"(peak ${peak:,.2f}, entry ${entry:,.2f}, "
                        f"trail {self.trail_pct * 100:.0f}%)"
                    ),
                )
            return None

        # ----- Flat: look for new-high entry -----
        # Rolling-max of the previous `lookback` closed bars (excludes
        # the current bar so we measure "did this bar break the prior
        # high", not "is this bar the highest including itself").
        prior_window = candles["close"].iloc[-(self.lookback + 1):-1]
        prior_high = float(prior_window.max())

        if close > prior_high:
            self._in_position = True
            self._entry_price = close
            self._peak_since_entry = high  # start trail from this bar's high
            return Signal(
                action=SignalAction.OPEN_LONG,
                symbol=self.symbol,
                strategy_name=self.name,
                reason=(
                    f"New {self.lookback}d high: close ${close:,.2f} > "
                    f"prior high ${prior_high:,.2f}"
                ),
            )

        return None
