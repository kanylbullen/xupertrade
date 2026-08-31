"""Differential parity tests: vectorized oleg_aryukov indicators vs legacy.

Backlog item "Optimize oleg_aryukov for backtest" (CLAUDE.md § 5, Open —
Low). The backtest runner replays candles with a growing window
(``df.iloc[: i + 1]`` per bar), so the former per-bar Python loops in
``_rci`` — one ``np.corrcoef`` call per window, three RCI lengths per bar —
extrapolate to ~100 minutes of CPU for a 4320-bar backtest.

HARD CONSTRAINT of the refactor: behavior-preserving. The vectorized
implementations must produce identical output (within 1e-9) for the same
candle window. These tests pin them to verbatim copies of the pre-refactor
code (the "legacy oracles" below) and prove, at three levels:

1. Unit, Nadaraya-Watson: ``_nadaraya_watson_series(closes)[t]`` equals the
   legacy scalar applied to the same prefix ``closes[: t + 1]`` — for every
   bar, which is exactly what the growing-window backtest used to compute
   one call at a time. Includes tie-heavy, constant, monotone and
   degenerate-input cases.
2. Unit, RCI: the vectorized series equals the legacy per-bar loop series,
   including NaN placement. The legacy loop's ``np.corrcoef`` of ordinal
   ranks vs. time ranks is exactly Spearman's closed form
   ``1 - 6*sum(d^2)/(L*(L^2-1))`` (both rank vectors are permutations of
   the same uniform grid), so differences are ~1e-13 in practice. Tie order
   matches because both implementations rank with numpy's default argsort,
   one window at a time.
3. End-to-end: stepping the full strategy through growing candle windows
   with the module patched back to the legacy oracles must yield the
   identical per-bar signal stream as the shipped vectorized path (guards
   against wiring regressions: wrong index, off-by-one, swapped args).

Tolerance: 1e-9 absolute, per the task's float-tolerance contract.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import hypertrade.strategies.oleg_aryukov as oleg_mod
from hypertrade.strategies.oleg_aryukov import (
    OlegAryukovStrategy,
    _nadaraya_watson,
    _nadaraya_watson_series,
    _rci,
)

TOL = 1e-9


# ---------------------------------------------------------------------------
# Legacy oracles — VERBATIM copies of the pre-refactor implementations.
# Source: bot/hypertrade/strategies/oleg_aryukov.py before branch
# refactor/oleg-aryukov-vectorize. Do not optimize or "clean up" these;
# they are the behavioral ground truth the vectorized code must reproduce.
# ---------------------------------------------------------------------------


def _nadaraya_watson_legacy(closes, bandwidth, lookback):
    """Pre-refactor scalar Nadaraya-Watson (last bar only)."""
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


def _rci_legacy(close, length):
    """Pre-refactor per-bar RCI loop (np.corrcoef per window)."""
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


def _nw_series_legacy_shim(closes, bandwidth, lookback):
    """Pre-refactor ``on_candle`` NW behavior as a series.

    The old code called the scalar oracle on whatever window ``on_candle``
    was handed; under the backtest's growing window that is exactly the
    oracle applied to ``closes[: t + 1]`` at bar ``t``.
    """
    return np.array(
        [
            _nadaraya_watson_legacy(closes[: t + 1], bandwidth, lookback)
            for t in range(len(closes))
        ],
        dtype=float,
    )


def _tsi_signal_source_legacy(ds_pc, ds_abs):
    """Pre-refactor TSI per-element ratio loop (the former list comp)."""
    return pd.Series(
        [
            0.0 if (ds_abs.iloc[i] == 0 or pd.isna(ds_abs.iloc[i])) else
            100.0 * ds_pc.iloc[i] / ds_abs.iloc[i]
            for i in range(len(ds_pc))
        ],
        index=ds_pc.index,
    )


# ---------------------------------------------------------------------------
# Data zoo — deterministic, seeded, adversarial for ranking/kernels.
# ---------------------------------------------------------------------------

def _data_zoo() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(20260831)
    return {
        "random_walk": 2500.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, 512))),
        # integer-quantized prices -> many exact ties -> pins argsort
        # tie-order parity between the 1-D (legacy) and 2-D (new) argsort
        "ties_heavy": np.round(rng.normal(100.0, 10.0, 512)).astype(float),
        "two_level": np.where(np.arange(512) % 7 == 0, 50.0, 100.0),
        "constant": np.full(256, 123.45),
        "monotone_up": np.linspace(1.0, 999.0, 256),
        "monotone_down": np.linspace(999.0, 1.0, 256),
        "sawtooth": np.tile(np.arange(9.0), 57),
        "single_spike": np.concatenate(
            [np.full(128, 10.0), [1_000_000.0], np.full(127, 10.0)]
        ),
    }


ZOO = _data_zoo()


# ---------------------------------------------------------------------------
# 1. Nadaraya-Watson: vectorized series vs per-bar legacy oracle
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lookback", [1, 2, 9, 50, 200])
@pytest.mark.parametrize("bandwidth", [0.5, 3.0, 100.0])
@pytest.mark.parametrize("dataset", sorted(ZOO))
def test_nw_series_matches_legacy_per_bar(dataset, bandwidth, lookback):
    closes = ZOO[dataset]
    got = _nadaraya_watson_series(closes, bandwidth, lookback)
    assert got.shape == (len(closes),)
    for t in range(len(closes)):
        # the legacy oracle applied to the prefix == one growing-window
        # backtest bar's computation
        expected = _nadaraya_watson_legacy(closes[: t + 1], bandwidth, lookback)
        if math.isnan(expected):
            assert math.isnan(got[t]), (dataset, bandwidth, lookback, t)
        else:
            assert abs(got[t] - expected) <= TOL, (dataset, bandwidth, lookback, t)


@pytest.mark.parametrize("lookback", [9, 50, 200])
@pytest.mark.parametrize("bandwidth", [0.5, 3.0, 100.0])
@pytest.mark.parametrize("dataset", sorted(ZOO))
def test_nw_series_last_bar_matches_reference_scalar(dataset, bandwidth, lookback):
    """The live call shape: series[-1] == unchanged scalar oracle."""
    closes = ZOO[dataset]
    expected = _nadaraya_watson(closes, bandwidth, lookback)
    got = float(_nadaraya_watson_series(closes, bandwidth, lookback)[-1])
    if math.isnan(expected):
        assert math.isnan(got)
    else:
        assert abs(got - expected) <= TOL


def test_nw_series_edge_cases():
    # empty input -> empty series (unreachable in on_candle: warmup guard)
    assert _nadaraya_watson_series(np.array([]), 3.0, 50).shape == (0,)
    # lookback <= 0 -> all NaN, matching the scalar oracle's n <= 0 -> NaN
    for closes in (np.array([1.0, 2.0, 3.0]), np.array([])):
        for lookback in (0, -5):
            out = _nadaraya_watson_series(closes, 3.0, lookback)
            assert len(out) == len(closes)
            assert np.isnan(out).all()
    # bandwidth == 0: weight[0] = exp(-0/0) = NaN -> NaN estimates in BOTH
    # implementations (numpy float division, no exception)
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    out = _nadaraya_watson_series(closes, 0.0, 4)
    for t in range(len(closes)):
        expected = _nadaraya_watson_legacy(closes[: t + 1], 0.0, 4)
        assert math.isnan(expected)
        assert math.isnan(out[t]), t


def test_nw_estimate_within_window_range():
    """Convexity invariant: positive weights summing to 1 keep the estimate
    inside [min, max] of the window it used."""
    closes = ZOO["random_walk"]
    ser = _nadaraya_watson_series(closes, 3.0, 50)
    for t in range(len(closes)):
        m = min(50, t + 1)
        window = closes[t + 1 - m : t + 1]
        assert window.min() - TOL <= ser[t] <= window.max() + TOL, t


# ---------------------------------------------------------------------------
# 2. RCI: vectorized rank correlation vs legacy per-bar loop
# ---------------------------------------------------------------------------

RCI_LENGTHS = [2, 3, 9, 26, 52, 64, 300]


@pytest.mark.parametrize("length", RCI_LENGTHS)
@pytest.mark.parametrize("dataset", sorted(ZOO))
def test_rci_matches_legacy_loop(dataset, length):
    close = pd.Series(ZOO[dataset])
    got = _rci(close, length)
    expected = _rci_legacy(close, length)
    assert isinstance(got, pd.Series)
    assert got.index.equals(close.index)
    got_v, exp_v = got.values, expected.values
    # identical NaN placement (warmup head, and length > n -> all NaN)
    assert (np.isnan(got_v) == np.isnan(exp_v)).all(), (dataset, length)
    finite = ~np.isnan(got_v)
    if finite.any():
        max_diff = float(np.max(np.abs(got_v[finite] - exp_v[finite])))
        assert max_diff <= TOL, (dataset, length, max_diff)


def test_rci_matches_legacy_loop_at_backtest_scale():
    """4320 bars (180d of 1h) at the strategy's slow length: same series to
    within 1e-9, in milliseconds instead of ~1s of corrcoef calls."""
    rng = np.random.default_rng(7)
    closes = pd.Series(2500.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, 4320))))
    got = _rci(closes, 52)
    expected = _rci_legacy(closes, 52)
    finite = ~np.isnan(expected.values)
    max_diff = float(np.max(np.abs(got.values[finite] - expected.values[finite])))
    assert max_diff <= TOL, max_diff


def test_rci_monotone_series_hit_exact_bounds():
    """Strictly monotone windows: ranks align with (or invert) time ranks
    exactly, so RCI saturates at +-100 — in both implementations."""
    up = pd.Series(np.linspace(1.0, 999.0, 120))
    down = pd.Series(np.linspace(999.0, 1.0, 120))
    for length in (9, 26, 52):
        got_up = _rci(up, length).values[length - 1 :]
        got_down = _rci(down, length).values[length - 1 :]
        assert (got_up == 100.0).all()  # d == 0 -> corr = 1 exactly
        assert (got_down == -100.0).all()  # 6*d2/denom == 2 exactly
        # legacy agrees within tolerance
        legacy_up = _rci_legacy(up, length).values[length - 1 :]
        legacy_down = _rci_legacy(down, length).values[length - 1 :]
        assert np.max(np.abs(legacy_up - got_up)) <= TOL
        assert np.max(np.abs(legacy_down - got_down)) <= TOL


def test_rci_range_and_nan_head():
    close = pd.Series(ZOO["random_walk"])
    for length in (9, 26, 52):
        out = _rci(close, length).values
        assert np.isnan(out[: length - 1]).all()
        tail = out[length - 1 :]
        assert np.isfinite(tail).all()
        assert (np.abs(tail) <= 100.0 + TOL).all()


def test_rci_edge_cases():
    close = pd.Series([1.0, 2.0, 3.0])
    for length in (1, 0, -3):
        out = _rci(close, length)
        assert len(out) == 3 and np.isnan(out).all()
        assert out.index.equals(close.index)
    # n < length -> all NaN
    out = _rci(pd.Series([1.0, 2.0]), 9)
    assert len(out) == 2 and np.isnan(out).all()
    # empty input -> empty series
    assert len(_rci(pd.Series([], dtype=float), 9)) == 0


def test_rci_nan_input_parity():
    """Candle closes are NaN-free in practice; pin the deterministic
    behavior anyway (argsort sorts NaN last, identically in 1-D and 2-D)."""
    s = pd.Series([1.0, np.nan, 3.0, 2.0, 5.0, 4.0, 8.0, 7.0, 6.0, 0.5])
    a = _rci(s, 4).values
    b = _rci_legacy(s, 4).values
    assert (np.isnan(a) == np.isnan(b)).all()
    finite = ~np.isnan(a)
    assert np.max(np.abs(a[finite] - b[finite])) <= TOL


def test_rci_tail_slice_matches_full_series():
    """Warrant for the on_candle call sites: RCI at bar t depends only on
    the trailing `length` bars, so feeding _rci exactly the trailing window
    yields the identical last value as the full-history series — bit for
    bit, because both paths evaluate the same window through the same
    vectorized row computation."""
    for dataset, closes in ZOO.items():
        s = pd.Series(closes)
        for length in (2, 9, 26, 52):
            if len(s) < length:
                continue
            full_last = _rci(s, length).iloc[-1]
            tail_last = _rci(s.iloc[-length:], length).iloc[-1]
            if pd.isna(full_last):
                assert pd.isna(tail_last), (dataset, length)
            else:
                assert full_last == tail_last, (dataset, length)


# ---------------------------------------------------------------------------
# 2b. TSI ratio source: vectorized mapping vs legacy per-element loop
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dataset", sorted(ZOO))
def test_tsi_signal_source_matches_legacy_loop(dataset):
    """ds_pc/ds_abs built exactly as on_candle builds them (double
    pta.ema): NaN warmup head plus finite tail; constant/flat datasets
    drive ds_abs to exactly 0.0, exercising the zero mask."""
    import pandas_ta as pta  # noqa: WPS433 — same lib the strategy uses

    pc = pd.Series(ZOO[dataset]).diff()
    ds_pc = pta.ema(pta.ema(pc, length=25), length=13)
    ds_abs = pta.ema(pta.ema(pc.abs(), length=25), length=13)
    got = oleg_mod._tsi_signal_source(ds_pc, ds_abs)
    expected = _tsi_signal_source_legacy(ds_pc, ds_abs)
    assert isinstance(got, pd.Series)
    assert got.index.equals(ds_pc.index)
    got_v, exp_v = got.values, expected.values
    assert (np.isnan(got_v) == np.isnan(exp_v)).all(), dataset
    finite = ~np.isnan(got_v)
    if finite.any():
        max_diff = float(np.max(np.abs(got_v[finite] - exp_v[finite])))
        assert max_diff <= TOL, (dataset, max_diff)


def test_tsi_signal_source_edge_cases():
    # all-zero ds_abs -> every bar masked to 0.0 (even where ds_pc is NaN)
    zeros = pd.Series([0.0, 0.0, 0.0, 0.0])
    pc = pd.Series([np.nan, 1.0, -2.0, 3.0])
    got = oleg_mod._tsi_signal_source(pc, zeros)
    assert (got.values == 0.0).all()
    # NaN ds_abs -> masked to 0.0 (legacy: pd.isna branch)
    ab = pd.Series([np.nan, 0.0, 2.0, 4.0])
    pc = pd.Series([1.0, 2.0, 3.0, -4.0])
    got = oleg_mod._tsi_signal_source(pc, ab)
    expected = _tsi_signal_source_legacy(pc, ab)
    assert got.values[0] == 0.0 and got.values[1] == 0.0
    assert np.array_equal(got.values, expected.values)
    # negative-zero denominator behaves like zero in both
    ab = pd.Series([-0.0, 1.0])
    pc = pd.Series([5.0, 2.0])
    assert np.array_equal(
        oleg_mod._tsi_signal_source(pc, ab).values,
        _tsi_signal_source_legacy(pc, ab).values,
    )


# ---------------------------------------------------------------------------
# 3. End-to-end: on_candle signal stream, vectorized vs legacy-wired
# ---------------------------------------------------------------------------

WARMUP = 220  # the strategy's own warmup constant (max(..., 200) + 20)


def _make_candles(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 2500.0 * np.exp(np.cumsum(rng.normal(0.0, 0.008, n)))
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.0015, n)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.0015, n)))
    open_ = np.concatenate([[2500.0], close[:-1]])
    ts = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.abs(rng.normal(1000.0, 100.0, n)),
            "timestamp": ts,
        }
    )


async def _stream_signals(strat: OlegAryukovStrategy, candles: pd.DataFrame) -> list:
    """Replay the backtest runner's growing window through the strategy."""
    sigs = []
    for i in range(WARMUP, len(candles)):
        sigs.append(await strat.on_candle(candles.iloc[: i + 1]))
    return sigs


def _signal_fields(sig):
    """Comparable projection of a Signal (timestamp is auto-now, excluded)."""
    if sig is None:
        return None
    return (
        sig.action,
        sig.symbol,
        sig.size,
        sig.price,
        sig.stop_loss,
        sig.take_profit,
        sig.strategy_name,
        sig.reason,
    )


async def _assert_stream_parity(candles: pd.DataFrame, strat_kwargs: dict,
                                monkeypatch) -> None:
    new_sigs = [
        _signal_fields(s)
        for s in await _stream_signals(OlegAryukovStrategy(**strat_kwargs), candles)
    ]

    # Re-wire the module to the pre-refactor oracles — the exact behavior
    # the strategy had before the vectorization — and compare fresh runs.
    # (The RCI trailing-window slicing at the call sites is separately
    # proven by test_rci_tail_slice_matches_full_series; patching _rci with
    # the legacy full-series implementation here still yields the exact
    # original per-bar values because only iloc[-1] is consumed.)
    monkeypatch.setattr(oleg_mod, "_rci", _rci_legacy)
    monkeypatch.setattr(oleg_mod, "_nadaraya_watson_series", _nw_series_legacy_shim)
    monkeypatch.setattr(oleg_mod, "_tsi_signal_source", _tsi_signal_source_legacy)
    try:
        old_sigs = [
            _signal_fields(s)
            for s in await _stream_signals(OlegAryukovStrategy(**strat_kwargs), candles)
        ]
    finally:
        monkeypatch.undo()

    assert len(new_sigs) == len(old_sigs) == len(candles) - WARMUP
    for i, (new, old) in enumerate(zip(new_sigs, old_sigs)):
        if new is None or old is None:
            assert new is None and old is None, (
                f"bar {i}: legacy={old!r} vectorized={new!r}"
            )
            continue
        assert new[0] == old[0], f"bar {i}: action {old[0]} -> {new[0]}"
        assert new[1] == old[1], f"bar {i}: symbol"
        assert new[6] == old[6], f"bar {i}: strategy_name"
        assert new[7] == old[7], f"bar {i}: reason {old[7]!r} -> {new[7]!r}"
        for j in (2, 3, 4, 5):  # size, price, stop_loss, take_profit
            if new[j] is None or old[j] is None:
                assert new[j] is None and old[j] is None, f"bar {i}: field {j}"
            else:
                assert abs(new[j] - old[j]) <= TOL, f"bar {i}: field {j}"


@pytest.mark.asyncio
async def test_on_candle_stream_parity_default_config(monkeypatch):
    """Default strategy config over a growing window: every bar's signal
    (or None) must match the pre-refactor implementation exactly."""
    await _assert_stream_parity(_make_candles(256, seed=42), {}, monkeypatch)


@pytest.mark.asyncio
async def test_on_candle_stream_parity_alt_config(monkeypatch):
    """Different thresholds/toggles: parity must hold for any config, not
    just defaults (min_confirmations=2 lets more votes through)."""
    await _assert_stream_parity(
        _make_candles(248, seed=7),
        {"min_confirmations": 2, "check_trend": False, "use_trailing": False},
        monkeypatch,
    )


@pytest.mark.asyncio
async def test_on_candle_rci_calls_use_trailing_window_only(monkeypatch):
    """Warrant for the call-site slices: every _rci call from on_candle must
    receive exactly `length` bars (the trailing window), not the full
    history — the value-equivalence of that slice is proven by
    test_rci_tail_slice_matches_full_series; this pins the wiring."""
    calls: list[tuple[int, int]] = []
    real_rci = oleg_mod._rci

    def spy(close, length):
        calls.append((len(close), length))
        return real_rci(close, length)

    monkeypatch.setattr(oleg_mod, "_rci", spy)
    strat = OlegAryukovStrategy()
    candles = _make_candles(240, seed=5)
    await _stream_signals(strat, candles)
    monkeypatch.undo()

    assert calls, "expected on_candle to invoke _rci"
    assert {length for _, length in calls} == {
        strat.rci_fast, strat.rci_medium, strat.rci_slow,
    }
    assert all(n == length for n, length in calls), (
        f"_rci received window sizes != length: {sorted(set(calls))}"
    )
