"""Unit tests for the btc_ath_breakout HODL signal."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

import pandas as pd
import pytest

from hypertrade.hodl.btc_ath_breakout import BtcAthBreakoutSignal


def _candles(closes: list[float]) -> pd.DataFrame:
    """Build a 1d DataFrame with the given closes; OHL track close ±0.1%."""
    n = len(closes)
    base_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    timestamps = [base_ts + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "volume": [1000.0] * n,
        "timestamp": timestamps,
    })


@pytest.mark.asyncio
async def test_triggers_on_fresh_break_today():
    """Latest bar closes above prior 100d high → triggered, score 1.0."""
    sig = BtcAthBreakoutSignal()
    closes = [100.0] * 110  # 110 bars of flat $100
    closes[-1] = 120.0  # final bar breaks the plateau
    df = _candles(closes)
    with patch(
        "hypertrade.hodl.btc_ath_breakout.fetch_candles",
        AsyncMock(return_value=df),
    ):
        state = await sig.evaluate()
    assert state.triggered is True
    assert state.score == 1.0
    assert "Add now" in state.verdict
    assert "TODAY" in state.verdict


@pytest.mark.asyncio
async def test_triggers_within_recent_window():
    """Break happened 3 days ago, still within 7d window → triggered."""
    sig = BtcAthBreakoutSignal()
    closes = [100.0] * 110
    closes[-4] = 120.0  # break 3 bars ago (offset 3 from latest)
    closes[-3] = 115.0
    closes[-2] = 113.0
    closes[-1] = 110.0
    df = _candles(closes)
    with patch(
        "hypertrade.hodl.btc_ath_breakout.fetch_candles",
        AsyncMock(return_value=df),
    ):
        state = await sig.evaluate()
    assert state.triggered is True
    assert "3d ago" in state.verdict


@pytest.mark.asyncio
async def test_no_trigger_when_below_prior_high():
    """No bar in last 7 days closed above prior 100d high → not triggered."""
    sig = BtcAthBreakoutSignal()
    # Build an explicit prior peak in the lookback window so "prior high"
    # is clearly above latest bars.
    closes = [100.0] * 110
    closes[20] = 150.0  # historical peak — sets a high bar to clear
    # Last 7 bars are all ~100, well below 150
    df = _candles(closes)
    with patch(
        "hypertrade.hodl.btc_ath_breakout.fetch_candles",
        AsyncMock(return_value=df),
    ):
        state = await sig.evaluate()
    assert state.triggered is False
    assert state.score == 0.0
    assert "Wait" in state.verdict


@pytest.mark.asyncio
async def test_no_trigger_when_break_outside_recent_window():
    """Break happened 10 days ago — outside 7d window → not triggered."""
    sig = BtcAthBreakoutSignal()
    closes = [100.0] * 120
    # Set a peak in the middle, then bar at offset 10 breaks it,
    # but no break since.
    closes[40] = 150.0
    closes[-11] = 200.0  # break 10 bars ago, outside default 7d window
    # Bars after must NOT exceed 200 (otherwise they'd be a fresh break)
    for i in range(-10, 0):
        closes[i] = 180.0
    df = _candles(closes)
    with patch(
        "hypertrade.hodl.btc_ath_breakout.fetch_candles",
        AsyncMock(return_value=df),
    ):
        state = await sig.evaluate()
    assert state.triggered is False
    assert "Wait" in state.verdict


@pytest.mark.asyncio
async def test_handles_fetch_failure():
    """If candle fetch raises, return error state — never raise."""
    sig = BtcAthBreakoutSignal()
    with patch(
        "hypertrade.hodl.btc_ath_breakout.fetch_candles",
        AsyncMock(side_effect=RuntimeError("network down")),
    ):
        state = await sig.evaluate()
    assert state.triggered is False
    assert state.error is not None
    assert "network down" in state.error
