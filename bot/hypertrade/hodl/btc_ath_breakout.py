"""BTC ATH-breakout HODL signal — flag fresh 100d-high breaks for manual
cold-storage accumulation.

Companion to the auto-trading `ath_breakout` strategy, but with NO exit
logic. Use case: "should I add to my long-term BTC stack right now?"
The auto-trade strategy answers a different question ("should the bot
deploy tactical capital now?") and uses a 35% trail to manage drawdown.
The HODL stack should never be sold by the bot — this signal just
notifies you, and you add manually.

Trigger: BTC closed above its prior 100-day high within the last 7
trading days. The 7-day window prevents the verdict from flipping on
every single bar (which would spam Telegram on transitions).

Backtest evidence (5y BTC 1d, 2021-05 → 2026-05, $10k single deploy at
the FIRST trigger held to end of window):
    HODL (buy at window start)         +36.6%   APR 6.4%
    DCA monthly (full window)          +90.8%   APR 13.8%
    Buy at first ATH break, never sell +250.9%  APR 33.8%

The signal works because it sits flat through bear regimes (no fresh
100d high broken during downtrends) and triggers when a regime change
is confirmed by price action.
"""

from __future__ import annotations

import logging

from hypertrade.data.feed import fetch_candles
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


@register
class BtcAthBreakoutSignal(Signal):
    name = "btc_ath_breakout"
    asset = "BTC"
    description = (
        "Fresh 100-day high broken in the last 7 days. Designed as a "
        "trigger for manual additions to the long-term BTC stack — "
        "advisory only, no auto-trading. Companion to the ath_breakout "
        "auto-trade strategy which manages tactical capital with a "
        "trailing stop."
    )
    threshold = 0.5

    lookback: int = 100
    # Window of recent bars to scan for a break. Wider than 1 so the
    # verdict stays stable for a few days after a break instead of
    # flipping back to "no" the day after — which would spam Telegram
    # via the runner's verdict-change notifier.
    recent_bars: int = 7

    async def evaluate(self) -> SignalState:
        try:
            # lookback + recent_bars + buffer
            btc = await fetch_candles("BTC", "1d", limit=self.lookback + 30)
        except Exception as e:
            logger.exception("btc_ath_breakout: candle fetch failed")
            return self._build_state([], error=f"candle fetch failed: {e}")

        if btc is None or btc.empty or len(btc) < self.lookback + self.recent_bars + 1:
            return self._build_state(
                [],
                error=f"need at least {self.lookback + self.recent_bars + 1} bars",
            )

        closes = btc["close"].astype(float)
        latest_close = float(closes.iloc[-1])

        # Scan the last `recent_bars` for any break of the prior `lookback`
        # high. For each candidate bar i, "prior high" excludes that bar
        # itself (we measure "did THIS bar break the prior window"?).
        break_bar_offset: int | None = None
        break_close: float | None = None
        break_prior_high: float | None = None
        for offset in range(self.recent_bars):
            i = len(closes) - 1 - offset  # 0 = today, 1 = yesterday, ...
            prior_high = float(closes.iloc[i - self.lookback : i].max())
            if float(closes.iloc[i]) > prior_high:
                break_bar_offset = offset
                break_close = float(closes.iloc[i])
                break_prior_high = prior_high
                break  # find the most recent break, then stop

        recent_break_check = Check(
            name=f"Fresh {self.lookback}d high in last {self.recent_bars} days",
            passed=break_bar_offset is not None,
            value=(
                f"Broke at close ${break_close:,.0f} > prior high "
                f"${break_prior_high:,.0f} ({break_bar_offset}d ago)"
                if break_bar_offset is not None
                else f"No close above prior {self.lookback}d high "
                     f"in last {self.recent_bars} days "
                     f"(current ${latest_close:,.0f}, prior high "
                     f"${float(closes.iloc[-(self.lookback + 1):-1].max()):,.0f})"
            ),
            threshold=f"close > {self.lookback}d prior high",
        )

        if break_bar_offset is not None:
            verdict = (
                f"Add now — fresh {self.lookback}d high "
                f"({break_bar_offset}d ago)"
                if break_bar_offset > 0
                else f"Add now — fresh {self.lookback}d high TODAY"
            )
            return SignalState(
                name=self.name,
                asset=self.asset,
                description=self.description,
                triggered=True,
                score=1.0,
                threshold=self.threshold,
                verdict=verdict,
                checks=[recent_break_check],
                notes=(
                    "Manual action: add to long-term BTC stack. "
                    "This signal never triggers an auto-sell."
                ),
            )

        return SignalState(
            name=self.name,
            asset=self.asset,
            description=self.description,
            triggered=False,
            score=0.0,
            threshold=self.threshold,
            verdict="Wait — no fresh ATH break",
            checks=[recent_break_check],
            notes=(
                f"Will trigger when BTC closes above its prior "
                f"{self.lookback}d high."
            ),
        )
