"""Unit tests for the four HyperTrade trading strategies.

Tests are grouped by strategy and cover:
1. Warm-up guard  — on_candle returns None when candles < minimum history
2. Entry signal   — synthetic OHLCV that satisfies all indicator conditions
3. restore_state  — no immediate close on the first tick after restore
4. SL exit        — a candle whose low/high crosses the stop-loss fires CLOSE_*
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from hypertrade.engine.signals import SignalAction
from hypertrade.strategies.ema_crossover import EMACrossoverStrategy
from hypertrade.strategies.btc_mean_reversion import BTCMeanReversionStrategy
from hypertrade.strategies.keltner_breakout import KeltnerBreakoutStrategy
from hypertrade.strategies.volatility_breakout import VolatilityBreakoutStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(i: int) -> datetime:
    """Return a UTC datetime offset by i hours from epoch."""
    return datetime(2024, 1, 1, i % 24, 0, 0, tzinfo=timezone.utc)


def _flat_df(n: int, price: float = 100.0, volume: float = 1000.0) -> pd.DataFrame:
    """Return an n-row OHLCV DataFrame with constant values and timestamps."""
    return pd.DataFrame(
        {
            "open": price,
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "volume": volume,
            "timestamp": [_ts(i) for i in range(n)],
        }
    )


def _append_row(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Append a single row (dict) to df and reset the index."""
    last_ts = df["timestamp"].iloc[-1]
    row = {
        "open": kwargs.get("open", float(df["close"].iloc[-1])),
        "high": kwargs.get("high", float(df["close"].iloc[-1]) * 1.001),
        "low": kwargs.get("low", float(df["close"].iloc[-1]) * 0.999),
        "close": kwargs.get("close", float(df["close"].iloc[-1])),
        "volume": kwargs.get("volume", float(df["volume"].iloc[-1])),
        "timestamp": _ts(len(df)),
    }
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


# ===========================================================================
# EMA Crossover Strategy
# ===========================================================================

class TestEMACrossover:
    """Tests for EMACrossoverStrategy (fast=7, slow=19, sl_candles=4)."""

    # Minimum rows required: slow_len + sl_candles + 5 = 19 + 4 + 5 = 28
    WARMUP = EMACrossoverStrategy.slow_len + EMACrossoverStrategy.sl_candles + 5

    @pytest.mark.asyncio
    async def test_warmup_guard(self):
        """on_candle returns None when fewer than WARMUP candles are provided."""
        strat = EMACrossoverStrategy()
        df = _flat_df(self.WARMUP - 1)
        result = await strat.on_candle(df)
        assert result is None

    @pytest.mark.asyncio
    async def test_bullish_cross_fires_open_long(self):
        """A bullish EMA crossover emits OPEN_LONG.

        The strategy uses closed=df.iloc[:-1], checks closed.iloc[-2] (prev)
        and closed.iloc[-1] (latest), and fires when:
            prev_fast <= prev_slow  AND  cur_fast > cur_slow

        Shape:
          - 30 bars at 100  → both EMAs settle at 100
          - 5 bars at 80    → fast EMA drops below slow EMA (bearish divergence)
          - 2 bars at 130   → fast EMA rockets back above slow EMA
        Total = 37 rows; closed = 36 rows; cross at closed[-2]→closed[-1].
        Verified analytically: at row 34 fast(84.7)<slow(91.8), at row 35 fast(96.1)>slow(95.6).
        """
        strat = EMACrossoverStrategy()
        # 30 flat bars anchor both EMAs at 100
        prices = [100.0] * 30
        # 5 bars at 80 push fast EMA below slow EMA
        prices += [80.0] * 5
        # 2 spike bars at 130: closed[-2]→closed[-1] is the bullish cross
        prices += [130.0] * 2
        df = pd.DataFrame(
            {
                "open": prices,
                "high": [p * 1.001 for p in prices],
                "low": [p * 0.999 for p in prices],
                "close": prices,
                "volume": 1000.0,
                "timestamp": [_ts(i) for i in range(len(prices))],
            }
        )
        result = await strat.on_candle(df)
        assert result is not None
        assert result.action == SignalAction.OPEN_LONG

    @pytest.mark.asyncio
    async def test_restore_state_no_immediate_close(self):
        """After restore_state, a normal candle does NOT close the position.

        The strategy recomputes SL = min(closed['low'].iloc[-4:]) which uses
        df[-5] through df[-2] lows.  The check is: if low (=df[-2].low) <= SL.
        To avoid triggering the SL the last 4 lows must be LOWER than the
        current 'latest' low.  We achieve this by setting the last 5 rows'
        lows to 90 and the final 'open' bar (df[-1]) low to 100.
        """
        strat = EMACrossoverStrategy()
        entry_price = 100.0
        strat.restore_state("long", entry_price)

        n = self.WARMUP + 10
        # All candles at 110 — comfortable above entry
        df = _flat_df(n, price=110.0)
        # Force the rows that become closed[-4:] (= df[-6:-2]) to have low=90
        # so SL = 90, and closed[-1] (= df[-2]) has low=100 > 90 → no close
        for idx in [-6, -5, -4, -3]:
            df.at[df.index[idx], "low"] = 90.0
        # closed[-1] = df[-2]: set low=100 so low(100) > SL(90)
        df.at[df.index[-2], "low"] = 100.0
        df.at[df.index[-2], "close"] = 110.0
        df.at[df.index[-2], "high"] = 115.0

        result = await strat.on_candle(df)
        assert result is None

    @pytest.mark.asyncio
    async def test_sl_exit_closes_long(self):
        """After restore_state, a candle with low < SL triggers CLOSE_LONG."""
        strat = EMACrossoverStrategy()
        entry_price = 100.0
        strat.restore_state("long", entry_price)

        # Build a frame; SL will be recomputed as min(low[-4:]).
        # Keep the low of the last 4 rows at 90.0.
        n = self.WARMUP + 5
        df = _flat_df(n, price=100.0)
        # Force the last 4 lows to 90.0 so SL = 90.0 after restore recompute
        for i in range(-4, 0):
            df.at[df.index[i], "low"] = 90.0

        # Now add a candle with low = 89.0 (below SL of 90.0)
        df = _append_row(df, close=99.0, high=101.0, low=89.0)

        result = await strat.on_candle(df)
        assert result is not None
        assert result.action == SignalAction.CLOSE_LONG


# ===========================================================================
# BTC Mean Reversion Strategy
# ===========================================================================

class TestBTCMeanReversion:
    """Tests for BTCMeanReversionStrategy (ema=200, rsi<20, stoch_k<25)."""

    WARMUP = BTCMeanReversionStrategy.ema_length + 5  # 205

    @pytest.mark.asyncio
    async def test_warmup_guard(self):
        strat = BTCMeanReversionStrategy()
        df = _flat_df(self.WARMUP - 1)
        result = await strat.on_candle(df)
        assert result is None

    @pytest.mark.asyncio
    async def test_long_entry_signal(self):
        """RSI<20 + stoch_k<25 + close > EMA200*0.9 fires OPEN_LONG.

        With 200+ bars at 100 the EMA200 settles near 100.  Dropping to 92
        satisfies: RSI→0 (all candles fell), stoch_k→~1, and
        close(92) > EMA200(~99)*0.9(~89).  A 50-bar crash fails the
        close>EMA*0.9 check because the EMA hasn't decayed enough.
        """
        strat = BTCMeanReversionStrategy()
        # 205 bars at 100, then 10 bars at 92 (moderate drop, stays above 0.9×EMA)
        n = 215
        prices = [100.0] * (n - 10) + [92.0] * 10
        df = pd.DataFrame(
            {
                "open": prices,
                "high": [p * 1.001 for p in prices],
                "low": [p * 0.999 for p in prices],
                "close": prices,
                "volume": 1000.0,
                "timestamp": [_ts(i) for i in range(n)],
            }
        )
        result = await strat.on_candle(df)
        assert result is not None
        assert result.action == SignalAction.OPEN_LONG

    @pytest.mark.asyncio
    async def test_restore_state_no_immediate_close(self):
        """After restore_state for a long, a normal safe candle returns None."""
        entry_price = 100.0
        strat = BTCMeanReversionStrategy()
        strat.restore_state("long", entry_price)

        # SL = entry * (1 - 0.04) = 96.0; TP = None (not set by restore_state).
        # Build a candle well above SL.
        n = self.WARMUP + 5
        df = _flat_df(n, price=entry_price)
        # last candle: low above SL
        df.at[df.index[-1], "low"] = entry_price * 0.97  # 97 > 96 SL — safe
        df.at[df.index[-1], "high"] = entry_price * 1.01
        result = await strat.on_candle(df)
        # Should return None because low > SL and TP not set
        assert result is None

    @pytest.mark.asyncio
    async def test_sl_exit_closes_long(self):
        """After restore_state, candle with low below SL triggers CLOSE_LONG."""
        entry_price = 100.0
        strat = BTCMeanReversionStrategy()
        strat.restore_state("long", entry_price)
        # SL = 100 * 0.96 = 96.0

        n = self.WARMUP + 5
        df = _flat_df(n, price=entry_price)
        # Last candle: low = 95.0 < SL 96.0
        df.at[df.index[-1], "low"] = 95.0
        df.at[df.index[-1], "high"] = 101.0

        result = await strat.on_candle(df)
        assert result is not None
        assert result.action == SignalAction.CLOSE_LONG


# ===========================================================================
# Keltner Breakout Strategy
# ===========================================================================

class TestKeltnerBreakout:
    """Tests for KeltnerBreakoutStrategy (ema=200, kc=20, atr=14, long-only)."""

    # Minimum rows: ema_len + 20 = 220 (candles) + 1 (closed = df[:-1])
    WARMUP = KeltnerBreakoutStrategy.ema_len + 20 + 1  # 221

    @pytest.mark.asyncio
    async def test_warmup_guard(self):
        strat = KeltnerBreakoutStrategy()
        df = _flat_df(self.WARMUP - 1)
        result = await strat.on_candle(df)
        assert result is None

    @pytest.mark.asyncio
    async def test_breakout_fires_open_long(self):
        """Close above EMA200 and above upper KC fires OPEN_LONG."""
        strat = KeltnerBreakoutStrategy()
        n = 225
        # Stable trend: 220 bars at 100, then final bars spike upward
        prices = [100.0] * (n - 5) + [120.0] * 5  # spike well above EMA
        df = pd.DataFrame(
            {
                "open": prices,
                "high": [p * 1.005 for p in prices],
                "low": [p * 0.995 for p in prices],
                "close": prices,
                "volume": 1000.0,
                "timestamp": [_ts(i) for i in range(n)],
            }
        )
        result = await strat.on_candle(df)
        for extra in range(20):
            if result is not None:
                break
            prices.append(prices[-1] + 0.5)
            df = _append_row(
                df,
                close=prices[-1],
                high=prices[-1] * 1.005,
                low=prices[-1] * 0.995,
            )
            result = await strat.on_candle(df)

        assert result is not None
        assert result.action == SignalAction.OPEN_LONG

    @pytest.mark.asyncio
    async def test_restore_state_no_immediate_close(self):
        """After restore_state, a normal candle with low above SL returns None."""
        entry_price = 100.0
        strat = KeltnerBreakoutStrategy()
        strat.restore_state("long", entry_price)

        # SL = None; will be recomputed as entry - ATR*4 on first tick.
        # Keep the low of the synthetic candles well above a plausible SL.
        n = self.WARMUP + 5
        df = _flat_df(n, price=entry_price)
        # Flat price => ATR ≈ 0.2 (0.1% range * 100) => SL ≈ 100 - 0.2*4 = 99.2
        # Set close/high/low all > 99.5 so SL is not breached and no TP/KC exit
        df.at[df.index[-1], "low"] = entry_price * 0.998   # 99.8 > 99.2
        df.at[df.index[-1], "high"] = entry_price * 1.002
        df.at[df.index[-1], "close"] = entry_price * 1.001

        result = await strat.on_candle(df)
        assert result is None

    @pytest.mark.asyncio
    async def test_sl_exit_closes_long(self):
        """After restore_state, candle with very low 'low' triggers CLOSE_LONG."""
        entry_price = 100.0
        strat = KeltnerBreakoutStrategy()
        strat.restore_state("long", entry_price)

        n = self.WARMUP + 5
        # Use a slightly volatile frame so ATR is meaningful
        rng = np.random.default_rng(42)
        noise = rng.normal(0, 0.3, n)
        prices = 100.0 + np.cumsum(noise)
        prices = np.clip(prices, 80.0, 120.0)
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices * 1.002,
                "low": prices * 0.998,
                "close": prices,
                "volume": 1000.0,
                "timestamp": [_ts(i) for i in range(n)],
            }
        )
        # Force the last candle's low far below entry
        df.at[df.index[-1], "low"] = entry_price * 0.90  # 10% drop — well below any SL
        df.at[df.index[-1], "close"] = entry_price * 0.91
        df.at[df.index[-1], "high"] = entry_price * 0.92

        result = await strat.on_candle(df)
        assert result is not None
        assert result.action == SignalAction.CLOSE_LONG


# ===========================================================================
# Volatility Breakout Strategy
# ===========================================================================

class TestVolatilityBreakout:
    """Tests for VolatilityBreakoutStrategy (ema=220, kc=22, rsi=14, adx=14)."""

    # Minimum rows: ema_len + 5 = 225 (no closed slice unlike keltner)
    WARMUP = VolatilityBreakoutStrategy.ema_len + 5  # 225

    @pytest.mark.asyncio
    async def test_warmup_guard(self):
        strat = VolatilityBreakoutStrategy()
        df = _flat_df(self.WARMUP - 1)
        result = await strat.on_candle(df)
        assert result is None

    @pytest.mark.asyncio
    async def test_long_breakout_signal(self):
        """Keltner crossover with all filters satisfied fires OPEN_LONG."""
        strat = VolatilityBreakoutStrategy(
            use_trail=False,
            cooldown_hours=0,
        )
        # Build a frame: 230 bars of stable uptrend at 100, then a 20%
        # spike to force close above upper KC and above EMA220.
        n_base = 235
        prices = [100.0] * n_base
        df = pd.DataFrame(
            {
                "open": prices,
                "high": [p * 1.002 for p in prices],
                "low": [p * 0.998 for p in prices],
                "close": prices,
                "volume": [2000.0] * n_base,  # volume spike (above SMA18)
                "timestamp": [_ts(i) for i in range(n_base)],
            }
        )

        result = None
        # Keep appending rising candles until we get a signal
        for i in range(50):
            spike_close = 100.0 * (1.0 + 0.005 * (i + 1))
            df = _append_row(
                df,
                close=spike_close,
                high=spike_close * 1.01,
                low=spike_close * 0.99,
                volume=5000.0,
            )
            result = await strat.on_candle(df)
            if result is not None:
                break

        assert result is not None
        assert result.action == SignalAction.OPEN_LONG

    @pytest.mark.asyncio
    async def test_restore_state_no_immediate_close(self):
        """After restore_state, a candle that doesn't breach SL returns None."""
        entry_price = 100.0
        strat = VolatilityBreakoutStrategy()
        strat.restore_state("long", entry_price)

        # Build adequate history; keep low well above any ATR-based SL.
        n = self.WARMUP + 5
        df = _flat_df(n, price=entry_price)
        # Flat price => ATR ≈ tiny; SL = entry - tiny*4 ≈ just below entry
        # Ensure low stays above entry to be safe
        df.at[df.index[-1], "low"] = entry_price * 0.999
        df.at[df.index[-1], "high"] = entry_price * 1.001
        df.at[df.index[-1], "close"] = entry_price * 1.0005

        result = await strat.on_candle(df)
        # Must not fire a close
        assert result is None

    @pytest.mark.asyncio
    async def test_sl_exit_closes_long(self):
        """After restore_state, a candle with low far below SL fires CLOSE_LONG."""
        entry_price = 100.0
        strat = VolatilityBreakoutStrategy()
        strat.restore_state("long", entry_price)

        n = self.WARMUP + 5
        # Somewhat volatile frame so ATR > 0
        rng = np.random.default_rng(7)
        noise = rng.normal(0, 0.5, n)
        prices = 100.0 + np.cumsum(noise)
        prices = np.clip(prices, 70.0, 130.0)
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices * 1.003,
                "low": prices * 0.997,
                "close": prices,
                "volume": 1000.0,
                "timestamp": [_ts(i) for i in range(n)],
            }
        )
        # Force last candle low to crash far below entry (15% drop guaranteed
        # to breach entry - ATR*4 for any sane ATR)
        df.at[df.index[-1], "low"] = entry_price * 0.80
        df.at[df.index[-1], "close"] = entry_price * 0.82
        df.at[df.index[-1], "high"] = entry_price * 0.84

        result = await strat.on_candle(df)
        assert result is not None
        assert result.action == SignalAction.CLOSE_LONG
