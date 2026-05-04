"""Altseason signal — long-term rotation indicator.

Classical altseason setup:
  - BTC stable (not crashing)
  - ETH leading BTC (30d return advantage)
  - ETH/BTC ratio in uptrend
  - Multiple majors outperforming BTC (rotation breadth)
  - Macro risk-on (BTC > 200d SMA)

When ≥3 of 5 fire, alts have historically had a window where they
outperform BTC dollar-for-dollar. This is NOT a buy signal for any
specific token — it tells you the rotation environment is favorable,
so e.g. shifting some BTC weight into a basket of majors may pay off.
"""

from __future__ import annotations

import logging

import pandas as pd
import pandas_ta as pta

from hypertrade.data.feed import fetch_candles
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


def _pct_return(closes: pd.Series, days: int) -> float:
    """Percent return over last `days` periods."""
    if len(closes) < days + 1:
        return float("nan")
    start = float(closes.iloc[-(days + 1)])
    end = float(closes.iloc[-1])
    if start <= 0:
        return float("nan")
    return (end - start) / start * 100.0


@register
class AltseasonSignal(Signal):
    name = "altseason"
    asset = "ALT"
    description = (
        "Detects when conditions favor altcoin outperformance vs BTC: "
        "ETH leading, rotation breadth across majors, BTC stable, risk-on macro."
    )
    threshold = 0.6  # 3 of 5 checks

    return_window_days: int = 30
    eth_outperformance_pct: float = 5.0   # ETH must beat BTC by ≥ this over window
    btc_short_sma: int = 30                # BTC > SMA30 = short-term stable
    btc_long_sma: int = 200                # BTC > SMA200 = risk-on macro
    ethbtc_ma_length: int = 14
    # BNB is included as a proxy for Asia/CEX-driven flow — historically
    # leads broader altseasons since Binance retail tends to rotate first.
    breadth_alts: tuple[str, ...] = ("SOL", "BNB", "AVAX", "DOGE")
    breadth_min_outperformers: int = 2     # ≥ this many alts must beat BTC

    def _verdict(self, score: float) -> str:
        if score >= 0.8:
            return "Altseason — strong rotation"
        if score >= self.threshold:
            return "Rotating — alts gaining vs BTC"
        if score >= 0.4:
            return "Mixed — no clear leadership"
        return "BTC season — alts lagging"

    async def evaluate(self) -> SignalState:
        try:
            btc = await fetch_candles("BTC", "1d", limit=max(self.btc_long_sma + 5, 220))
            eth = await fetch_candles("ETH", "1d", limit=self.return_window_days + 30)
            alt_dfs: dict[str, pd.DataFrame] = {}
            for sym in self.breadth_alts:
                alt_dfs[sym] = await fetch_candles(sym, "1d", limit=self.return_window_days + 5)
        except Exception as e:
            logger.exception("altseason: candle fetch failed")
            return self._build_state([], error=f"candle fetch failed: {e}")

        if btc is None or btc.empty or len(btc) < self.btc_long_sma + 1:
            return self._build_state([], error="not enough BTC candles")
        if eth is None or eth.empty or len(eth) < self.return_window_days + 1:
            return self._build_state([], error="not enough ETH candles")

        # 1. BTC stable (not crashing). Pass if BTC close > SMA(30).
        btc_close = float(btc["close"].iloc[-1])
        btc_sma_short = pta.sma(btc["close"], length=self.btc_short_sma)
        btc_sma_short_now = float(btc_sma_short.iloc[-1])
        btc_stable = btc_close > btc_sma_short_now

        # 2. ETH leading BTC over the window.
        btc_ret = _pct_return(btc["close"], self.return_window_days)
        eth_ret = _pct_return(eth["close"], self.return_window_days)
        eth_advantage = eth_ret - btc_ret if not (pd.isna(btc_ret) or pd.isna(eth_ret)) else float("nan")
        eth_leads = (
            not pd.isna(eth_advantage)
            and eth_advantage >= self.eth_outperformance_pct
        )

        # 3. ETH/BTC ratio above its own moving average.
        # Align by index — both series use daily candles from now backwards,
        # so iloc[-N:] of each gives the same N most recent days.
        n = min(len(btc), len(eth), self.ethbtc_ma_length + 5)
        eth_close = eth["close"].iloc[-n:].reset_index(drop=True)
        btc_close_aligned = btc["close"].iloc[-n:].reset_index(drop=True)
        ethbtc = eth_close / btc_close_aligned
        ethbtc_ma = pta.sma(ethbtc, length=self.ethbtc_ma_length)
        ethbtc_now = float(ethbtc.iloc[-1])
        ethbtc_ma_now = float(ethbtc_ma.iloc[-1]) if ethbtc_ma is not None else float("nan")
        ethbtc_uptrend = (
            not pd.isna(ethbtc_ma_now)
            and ethbtc_now > ethbtc_ma_now
        )

        # 4. Rotation breadth: how many of the tracked alts beat BTC over the window?
        outperformers = []
        outperformer_returns = {}
        for sym, df in alt_dfs.items():
            if df is None or df.empty or len(df) < self.return_window_days + 1:
                continue
            r = _pct_return(df["close"], self.return_window_days)
            outperformer_returns[sym] = r
            if not pd.isna(r) and not pd.isna(btc_ret) and r > btc_ret:
                outperformers.append(sym)
        breadth_pass = len(outperformers) >= self.breadth_min_outperformers

        # 5. Macro risk-on: BTC > 200d SMA.
        btc_sma_long = pta.sma(btc["close"], length=self.btc_long_sma)
        btc_sma_long_now = float(btc_sma_long.iloc[-1])
        risk_on = btc_close > btc_sma_long_now

        checks = [
            Check(
                name="BTC stable (short-term)",
                passed=btc_stable,
                value=f"BTC ${btc_close:,.0f} {'>' if btc_stable else '<'} SMA{self.btc_short_sma} ${btc_sma_short_now:,.0f}",
                threshold=f"BTC > SMA{self.btc_short_sma}",
            ),
            Check(
                name="ETH leading BTC",
                passed=eth_leads,
                value=(
                    f"ETH {eth_ret:+.1f}% vs BTC {btc_ret:+.1f}% "
                    f"({eth_advantage:+.1f}pp over {self.return_window_days}d)"
                    if not pd.isna(eth_advantage) else "n/a"
                ),
                threshold=f"ETH ≥ BTC + {self.eth_outperformance_pct:.0f}pp",
            ),
            Check(
                name="ETH/BTC ratio rising",
                passed=ethbtc_uptrend,
                value=(
                    f"ETH/BTC = {ethbtc_now:.5f} {'>' if ethbtc_uptrend else '<'} "
                    f"MA{self.ethbtc_ma_length} {ethbtc_ma_now:.5f}"
                    if not pd.isna(ethbtc_ma_now) else "n/a"
                ),
                threshold=f"ETH/BTC > MA{self.ethbtc_ma_length}",
            ),
            Check(
                name="Rotation breadth",
                passed=breadth_pass,
                value=(
                    f"{len(outperformers)}/{len(alt_dfs)} alts beat BTC: "
                    + ", ".join(
                        f"{s} {outperformer_returns.get(s, 0):+.1f}%"
                        for s in alt_dfs
                    )
                ),
                threshold=f"≥ {self.breadth_min_outperformers} alts beat BTC",
            ),
            Check(
                name="Macro risk-on",
                passed=risk_on,
                value=f"BTC ${btc_close:,.0f} {'>' if risk_on else '<'} SMA{self.btc_long_sma} ${btc_sma_long_now:,.0f}",
                threshold=f"BTC > SMA{self.btc_long_sma}",
            ),
        ]

        notes = (
            "Altseason ≠ buy signal for any specific token. It indicates a "
            "favorable environment to shift weight from BTC into a basket "
            "of majors. Rotation can reverse abruptly — re-check weekly."
        )
        return self._build_state(checks, notes=notes)
