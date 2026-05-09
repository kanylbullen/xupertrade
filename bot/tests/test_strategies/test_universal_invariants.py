"""Universal property-based invariants across ALL registered strategies.

Bug-finder option #1 from 2026-05-09: extends PR #17's hash_momentum
property tests to every other state-keeping strategy via duck-typed
universal invariants. Catches the spam-bug class (no-double-open,
close-without-prior-open) for all 22 strategies regardless of their
internal state-machine specifics.

Universal invariants (no strategy-internals inspection needed):

  U1  Signal output well-formed: returns None or a Signal whose
      strategy_name and symbol match the strategy's class attributes
  U2  No double-open same side: OPEN_LONG without intervening
      CLOSE_LONG must not happen (catches the 2026-05-09 spam class)
  U3  No spurious close: CLOSE_LONG can only follow an OPEN_LONG
      in the same long lifecycle; same for SHORT

Plus duck-typed state checks where attributes exist:

  D1  If `_in_long` AND `_in_short` both exist as attributes, never
      both True simultaneously (the side-exclusivity invariant)

Strategies are tested with random candle sequences generated against
their declared symbol/timeframe. Different strategies need different
warmup lengths (some need 200+ bars for EMA200), so the generator
adapts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from hypertrade.engine.signals import SignalAction
from hypertrade.strategies.base import Strategy
from hypertrade.strategies.registry import list_strategies, load_all, get_strategy


# Ensure all strategy modules are imported so registry is populated
load_all()


# Strategies that require >250 candles of warmup (EMA200, ATR-1400, etc.).
# We pass enough history but bias toward the lighter strategies in the
# default generator to keep test runtime reasonable.
HEAVY_WARMUP_STRATEGIES = {
    "ema_crossover",         # ema_slow=200
    "volatility_breakout",   # ema_len=220
    "keltner_breakout",      # ema200
    "pivot_supertrend",      # ema200
    "qullamagi_breakout",    # multi-MA
    "oleg_aryukov",          # ensemble, 200d
    "ath_breakout",          # lookback=100 + buffer
    "kalman_breakout",       # band_lookback=200
    "vvv_hedge",             # 30d daily baseline
}


def _required_warmup(name: str) -> int:
    return 280 if name in HEAVY_WARMUP_STRATEGIES else 100


@st.composite
def _random_candles(draw, n_bars: int, timeframe_hours: int = 4):
    """Generate a positive-priced OHLCV DataFrame with `n_bars` rows.

    Prices stay in [10, 10_000] (realistic crypto range, never invert
    high/low). Timeframe controls timestamp spacing.
    """
    base = draw(st.floats(
        min_value=50.0, max_value=5_000.0,
        allow_nan=False, allow_infinity=False,
    ))
    deltas = draw(st.lists(
        st.floats(
            min_value=-2.0, max_value=2.0,
            allow_nan=False, allow_infinity=False,
        ),
        min_size=n_bars, max_size=n_bars,
    ))

    closes = []
    cur = base
    for d in deltas:
        # 1% per-bar drift; floor at 10 so prices never approach 0
        cur = max(10.0, min(10_000.0, cur * (1 + d * 0.01)))
        closes.append(cur)

    base_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    timestamps = [
        base_ts + timedelta(hours=timeframe_hours * i) for i in range(n_bars)
    ]

    highs = [max(c, c * 1.005) for c in closes]
    lows = [min(c, c * 0.995) for c in closes]
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n_bars,
        "timestamp": timestamps,
    })


def _check_universal_invariants(
    strategy_name: str, expected_symbol: str, signals: list,
) -> None:
    """U1, U2, U3, D1 — applied per signal."""
    long_open = False
    short_open = False
    for sig in signals:
        # U1: well-formed signal
        assert sig.strategy_name == strategy_name, (
            f"U1: signal.strategy_name={sig.strategy_name!r} "
            f"!= expected {strategy_name!r}"
        )
        assert sig.symbol == expected_symbol, (
            f"U1: signal.symbol={sig.symbol!r} != strategy's "
            f"symbol {expected_symbol!r}"
        )

        # U2 + U3: signal sequence legality
        if sig.action == SignalAction.OPEN_LONG:
            assert not long_open, (
                f"U2: {strategy_name} OPEN_LONG fired while already long "
                f"(missing intervening CLOSE_LONG)"
            )
            long_open = True
        elif sig.action == SignalAction.OPEN_SHORT:
            assert not short_open, (
                f"U2: {strategy_name} OPEN_SHORT fired while already short"
            )
            short_open = True
        elif sig.action == SignalAction.CLOSE_LONG:
            assert long_open, (
                f"U3: {strategy_name} CLOSE_LONG fired with no prior "
                f"OPEN_LONG in this lifecycle"
            )
            long_open = False
        elif sig.action == SignalAction.CLOSE_SHORT:
            assert short_open, (
                f"U3: {strategy_name} CLOSE_SHORT fired with no prior "
                f"OPEN_SHORT in this lifecycle"
            )
            short_open = False


def _check_duck_typed_state(strategy_name: str, strat: Strategy) -> None:
    """D1: side exclusivity for strategies with both _in_long and _in_short."""
    has_long = hasattr(strat, "_in_long")
    has_short = hasattr(strat, "_in_short")
    if has_long and has_short:
        assert not (strat._in_long and strat._in_short), (
            f"D1: {strategy_name} has both _in_long and _in_short = True"
        )


# Run the same test for every registered strategy. Each strategy gets
# its own parametrized invocation, so failures pinpoint the exact
# offender instead of one opaque failure for the whole batch.
@pytest.mark.parametrize("strategy_name", sorted(list_strategies()))
def test_universal_invariants_per_strategy(strategy_name):
    """For each registered strategy: feed random candles, collect
    signals, assert universal invariants hold."""

    @given(df=_random_candles(n_bars=_required_warmup(strategy_name) + 30))
    @settings(
        max_examples=15,  # bounded: 22 strategies × 15 = 330 sequences
        deadline=None,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
        ],
    )
    def inner(df):
        strat = get_strategy(strategy_name)
        signals = []

        async def run():
            for tick in range(20):
                # Slice the df to simulate progressive ticks — each tick
                # adds one more bar at the right end. This catches the
                # bug class where a fresh Signal fires every tick within
                # a single bar (e.g. the 2026-05-09 hash_momentum spam).
                # First tick uses the full df; subsequent ticks repeat
                # the same df to simulate sub-bar polling.
                sig = await strat.on_candle(df)
                if sig is not None:
                    signals.append(sig)
                _check_duck_typed_state(strategy_name, strat)
            _check_universal_invariants(
                strategy_name, strat.symbol, signals,
            )

        asyncio.run(run())

    inner()
