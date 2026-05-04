"""HYPE accumulation signal — long-term spot DCA helper.

Thesis: Hyperliquid's Assistance Fund buys back HYPE with ~97% of fees,
which makes the token reflexively undervalued during pullbacks IF the
broader regime is risk-on AND the protocol is still generating volume.

This signal does NOT trade. It tells you "now might be a moment to add
more HYPE to your spot stack" by combining 5 conditions. Triggered when
≥3 of 5 weighted checks pass.
"""

from __future__ import annotations

import logging

import pandas as pd
import pandas_ta as pta

from hypertrade.data.feed import fetch_candles
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


@register
class HypeAccumulationSignal(Signal):
    name = "hype_accumulation"
    asset = "HYPE"
    description = (
        "Combines pullback depth, RSI, BTC regime, and HYPE volume to "
        "flag good moments to add to a long-term HYPE spot stack."
    )
    threshold = 0.6  # 3 of 5 checks pass (60%)

    pullback_pct: float = 15.0      # min pullback from 30d high
    rsi_threshold: float = 40.0     # daily RSI must be ≤ this
    rsi_length: int = 14
    high_window_days: int = 30
    btc_sma_length: int = 200       # BTC must be > 200d SMA = risk-on regime
    volume_surge_mult: float = 1.5  # 3d avg volume vs 30d avg

    async def evaluate(self) -> SignalState:
        try:
            hype = await fetch_candles("HYPE", "1d", limit=60)
            btc = await fetch_candles("BTC", "1d", limit=210)
        except Exception as e:
            logger.exception("hype_accumulation: candle fetch failed")
            return self._build_state([], error=f"candle fetch failed: {e}")

        if hype is None or hype.empty or len(hype) < self.high_window_days:
            return self._build_state([], error="not enough HYPE candles")
        if btc is None or btc.empty or len(btc) < self.btc_sma_length + 1:
            return self._build_state([], error="not enough BTC candles")

        last_close = float(hype["close"].iloc[-1])
        recent_high = float(hype["high"].iloc[-self.high_window_days:].max())
        pullback = (recent_high - last_close) / recent_high * 100.0

        rsi_series = pta.rsi(hype["close"], length=self.rsi_length)
        rsi = float(rsi_series.iloc[-1]) if rsi_series is not None else float("nan")

        btc_close = float(btc["close"].iloc[-1])
        btc_sma = pta.sma(btc["close"], length=self.btc_sma_length)
        btc_sma_now = float(btc_sma.iloc[-1]) if btc_sma is not None else float("nan")
        btc_above_sma = btc_close > btc_sma_now

        vol_3d = float(hype["volume"].iloc[-3:].mean())
        vol_30d = float(hype["volume"].iloc[-30:].mean())
        vol_ratio = vol_3d / vol_30d if vol_30d > 0 else 0.0

        # 5th check: not too overheated. Avoid signaling when HYPE has
        # already rallied >20% in last 7 days from the recent low.
        recent_low_7d = float(hype["low"].iloc[-7:].min())
        rebound_pct = (last_close - recent_low_7d) / recent_low_7d * 100.0 if recent_low_7d > 0 else 0.0
        not_overheated = rebound_pct < 20.0

        checks = [
            Check(
                name="Pullback depth",
                passed=pullback >= self.pullback_pct,
                value=f"{pullback:.1f}% from 30d high (${recent_high:,.2f})",
                threshold=f"≥ {self.pullback_pct:.0f}%",
            ),
            Check(
                name="Oversold RSI",
                passed=not pd.isna(rsi) and rsi <= self.rsi_threshold,
                value=f"RSI({self.rsi_length}) = {rsi:.1f}" if not pd.isna(rsi) else "n/a",
                threshold=f"≤ {self.rsi_threshold:.0f}",
            ),
            Check(
                name="BTC risk-on regime",
                passed=btc_above_sma,
                value=f"BTC ${btc_close:,.0f} {'>' if btc_above_sma else '<'} SMA{self.btc_sma_length} ${btc_sma_now:,.0f}",
                threshold=f"BTC > SMA{self.btc_sma_length}",
            ),
            Check(
                name="Volume confirmation",
                passed=vol_ratio >= self.volume_surge_mult,
                value=f"3d/30d vol ratio = {vol_ratio:.2f}",
                threshold=f"≥ {self.volume_surge_mult:.1f}",
            ),
            Check(
                name="Not over-extended",
                passed=not_overheated,
                value=f"{rebound_pct:.1f}% from 7d low (${recent_low_7d:,.2f})",
                threshold="< 20%",
            ),
        ]

        notes = (
            "Buyback-vs-unlock flow not tracked yet — would require Assistance "
            "Fund address monitoring. Add when ready for v2."
        )
        return self._build_state(checks, notes=notes)
