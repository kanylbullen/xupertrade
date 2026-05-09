"""Property-based tests for strategy state-machine invariants.

Bug-finder option #3: hypothesis generates random candle sequences
(including sub-bar polling and bar-transition mixes that mimic the
2026-05-09 spam scenario), simulates the strategy step by step, and
asserts state-machine invariants hold throughout.

Properties tested on hash_momentum (representative of the
position-keeping family — long/short, SL/TP, cooldown):

  P1  Side exclusivity:    _in_long and _in_short never both True
  P2  Open ⇒ state present: in_position implies _entry, _sl, _tp set
  P3  Closed ⇒ state cleared: not in_position ⇒ exit_signal_just_fired OR
                              _entry/_sl/_tp may still hold previous values
                              (state is only cleared on reset_state())
  P4  No double-open:      consecutive OPEN_LONG without intervening
                           CLOSE_LONG must not happen
  P5  No spurious close:   CLOSE_LONG can only fire when _in_long was True
  P6  SL/TP geometry:      LONG: _sl ≤ _entry ≤ _tp; SHORT: inverse
  P7  Cooldown monotone:   _bars_since_close only increases (or resets to
                           0 on close), never advances by >1 per bar tx
  P8  reset_state() clean: after reset, all position fields are clean
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncio
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from hypothesis import assume

from hypertrade.engine.signals import SignalAction
from hypertrade.strategies.hash_momentum import HashMomentumStrategy


# -- Generators ---------------------------------------------------------------

@st.composite
def candle_sequence(draw, min_bars=80, max_bars=300, sub_ticks_per_bar_max=5):
    """Generate a randomized sequence of (df, expected_bar_index) pairs
    simulating sub-bar polling.

    The sequence consists of:
    1. Initial warmup history (≥80 bars to satisfy hash_momentum's
       mom_length*3+20 requirement)
    2. Random number of additional bar transitions, each with random
       number of sub-bar polling ticks (0 to sub_ticks_per_bar_max).
    """
    # Build initial flat-ish history with small noise
    n_warmup = draw(st.integers(min_value=min_bars, max_value=min_bars + 20))
    base = 100.0
    noise = draw(st.lists(
        st.floats(min_value=-2.0, max_value=2.0, allow_nan=False),
        min_size=n_warmup, max_size=n_warmup,
    ))
    closes = [base + n for n in noise]

    base_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    timestamps = [base_ts + timedelta(hours=4 * i) for i in range(n_warmup)]

    # Build initial DataFrame
    def _df_from(closes_list, timestamps_list):
        return pd.DataFrame({
            "open": closes_list,
            "high": [c * 1.005 for c in closes_list],
            "low": [c * 0.995 for c in closes_list],
            "close": closes_list,
            "volume": [1000.0] * len(closes_list),
            "timestamp": timestamps_list,
        })

    sequence = [_df_from(closes, timestamps)]

    # Generate random new-bar additions with sub-bar repeats
    n_new_bars = draw(st.integers(min_value=0, max_value=20))
    for _ in range(n_new_bars):
        sub_ticks = draw(st.integers(min_value=0, max_value=sub_ticks_per_bar_max))
        # Append one new bar
        delta = draw(st.floats(min_value=-5.0, max_value=5.0, allow_nan=False))
        new_close = closes[-1] + delta
        new_ts = timestamps[-1] + timedelta(hours=4)
        closes = closes + [new_close]
        timestamps = timestamps + [new_ts]
        df_new = _df_from(closes, timestamps)
        # Repeat the same df sub_ticks times to simulate sub-bar polling
        for _ in range(sub_ticks + 1):
            sequence.append(df_new)

    return sequence


# -- Property assertions ------------------------------------------------------

def _check_invariants(strat: HashMomentumStrategy, signals_so_far: list) -> None:
    """All invariants P1, P2, P6 — pure state checks. Run after each tick."""
    # P1: side exclusivity
    assert not (strat._in_long and strat._in_short), (
        "P1 violated: both _in_long and _in_short are True"
    )
    # P2: in_position ⇒ entry/sl/tp present
    if strat._in_long or strat._in_short:
        assert strat._entry is not None, "P2: in_position but _entry is None"
        assert strat._sl is not None, "P2: in_position but _sl is None"
        assert strat._tp is not None, "P2: in_position but _tp is None"
        # P6: SL/TP geometry
        if strat._in_long:
            assert strat._sl <= strat._entry, (
                f"P6 LONG: sl {strat._sl} not ≤ entry {strat._entry}"
            )
            assert strat._entry <= strat._tp, (
                f"P6 LONG: entry {strat._entry} not ≤ tp {strat._tp}"
            )
        else:  # short
            assert strat._tp <= strat._entry, (
                f"P6 SHORT: tp {strat._tp} not ≤ entry {strat._entry}"
            )
            assert strat._entry <= strat._sl, (
                f"P6 SHORT: entry {strat._entry} not ≤ sl {strat._sl}"
            )


def _check_signal_legality(action_history: list) -> None:
    """P4 + P5: signal sequence legality.

    Filter out None, then verify:
      - No two consecutive OPEN_LONG without intervening CLOSE_LONG
      - No two consecutive OPEN_SHORT without intervening CLOSE_SHORT
      - CLOSE_LONG only follows an OPEN_LONG (in the long lifecycle)
      - CLOSE_SHORT only follows an OPEN_SHORT
    """
    long_open = False
    short_open = False
    for action in action_history:
        if action == SignalAction.OPEN_LONG:
            assert not long_open, "P4 LONG: OPEN_LONG fired while already long"
            assert not short_open, (
                "P4: OPEN_LONG fired while in short — engine flip-detect "
                "should synthesize close first"
            )
            long_open = True
        elif action == SignalAction.OPEN_SHORT:
            assert not short_open, "P4 SHORT: OPEN_SHORT fired while already short"
            assert not long_open, "P4: OPEN_SHORT fired while in long"
            short_open = True
        elif action == SignalAction.CLOSE_LONG:
            assert long_open, "P5: CLOSE_LONG fired while not long"
            long_open = False
        elif action == SignalAction.CLOSE_SHORT:
            assert short_open, "P5: CLOSE_SHORT fired while not short"
            short_open = False


# -- Tests --------------------------------------------------------------------

@given(seq=candle_sequence())
@settings(
    max_examples=50,
    deadline=None,  # async test — hypothesis can't time properly
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_state_invariants_hold_across_random_sequences(seq):
    """P1, P2, P4, P5, P6: simulate random candle sequences and verify
    state machine never enters illegal state."""
    strat = HashMomentumStrategy()
    actions: list = []

    async def run() -> None:
        for df in seq:
            sig = await strat.on_candle(df)
            _check_invariants(strat, actions)
            if sig is not None:
                actions.append(sig.action)
                _check_invariants(strat, actions)
        _check_signal_legality(actions)

    asyncio.run(run())


@given(seq=candle_sequence())
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_cooldown_advances_by_at_most_one_per_call(seq):
    """P7: _bars_since_close never increases by more than 1 in a single
    on_candle call. The 2026-05-09 bug was per-tick `+= 1` which produced
    arbitrarily many advances when called rapidly on the same bar; this
    invariant catches that class regardless of bar-transition semantics.

    (Decreases are allowed — close-signal handlers reset the counter to 0.)
    """
    strat = HashMomentumStrategy()

    async def run() -> None:
        prev = strat._bars_since_close
        for df in seq:
            await strat.on_candle(df)
            cur = strat._bars_since_close
            if cur > prev:
                delta = cur - prev
                assert delta == 1, (
                    f"P7 violated: counter advanced by {delta} > 1 in one call"
                )
            prev = cur

    asyncio.run(run())


@given(seq=candle_sequence(min_bars=80, max_bars=120))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_reset_state_returns_clean_state(seq):
    """P8: after reset_state(), the strategy is identical to a fresh one."""
    strat = HashMomentumStrategy()

    async def run() -> None:
        for df in seq:
            await strat.on_candle(df)
        strat.reset_state()
        assert strat._in_long is False
        assert strat._in_short is False
        assert strat._entry is None
        assert strat._sl is None
        assert strat._tp is None
        assert strat._bars_since_close == 999
        assert strat._last_closed_bar_ts is None

    asyncio.run(run())
