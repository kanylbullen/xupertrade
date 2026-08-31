# oleg_aryukov backtest performance — vectorized NW + RCI (behavior-preserving)

Branch: `refactor/oleg-aryukov-vectorize`
Backlog: CLAUDE.md § 5, Open — Low — "Optimize `oleg_aryukov` for backtest.
Nadaraya-Watson kernel + RCI loops are O(n²). Fine for live (one call per
tick) but a 4k-bar backtest hangs >30 min."

> Differential-test evidence lives in
> `bot/tests/test_strategies/test_oleg_vectorized_parity.py` (268 cases)
> and is summarized below. This file is the PR-body link target.

## Problem

The backtest runner replays candles with a growing window
(`df.iloc[: i + 1]` per bar — `bot/hypertrade/backtest/runner.py`), so
`on_candle` re-derived indicators over the entire history on every bar:

- `_rci` ran a per-bar Python loop with one `np.corrcoef` call per window,
  three RCI lengths (9/26/52) per call. Measured per-call cost at a
  4,000-bar window: **~2.6 s**; a 4,320-bar replay extrapolates to
  **~100 minutes** of CPU — consistent with the reported ">30 min hang".
- The same per-bar-loop pattern existed in the TSI vote block: a list
  comprehension doing two pandas scalar `.iloc` lookups per history bar
  (16k `.iloc` calls per `on_candle` at k=4000, ~80% of the residual
  profiled time after RCI was fixed).
- The Nadaraya-Watson scalar itself was only O(lookback) per call
  (~0.1 s per replay), but its per-bar semantics are what needed a
  vectorized equivalent to keep the NW vote path vectorized end-to-end.

## Approach

1. **Nadaraya-Watson** — new `_nadaraya_watson_series()` computes the
   whole per-bar estimate series as a rolling weighted convolution:
   a `sliding_window_view` over the closes, reversed so column 0 is the
   most recent bar, multiplied by the fixed Gaussian kernel
   `exp(-i²/2σ²)` and normalized by the constant kernel mass. Warmup bars
   with less history than the kernel mirror the scalar oracle's
   variable-length window (`n = min(lookback, t+1)`). `on_candle`
   consumes the last element. The scalar `_nadaraya_watson` is kept as
   the per-bar reference oracle for the differential tests.
2. **RCI** — the per-bar loop is replaced by one 2-D `argsort` over the
   sliding-window view plus the closed form
   `rho = 1 − 6·Σd²/(L·(L²−1))`. That identity is exact: the price ranks
   are always a permutation of the same uniform grid as the time ranks,
   so Pearson(ranks, time) equals Spearman's rank correlation and the
   `6Σd²/(n(n²−1))` formula applies with no tie-handling. The legacy
   loop's `std(ranks) < 1e-12` zero-variance guard was unreachable (a
   permutation of 0..L−1 always has positive variance for L ≥ 2); this is
   documented in the function and exercised by the constant/ties cases in
   the tests. Tie order is preserved because both implementations rank
   with numpy's default (introsort) argsort one window at a time — the
   1-D and row-wise 2-D paths agree, proven on tie-heavy data.
   The `on_candle` call sites feed each `_rci` call exactly its own
   trailing `length` bars (RCI at bar t depends only on those), with the
   value-equality of that slice proven explicitly (see evidence table).
3. **TSI** — the per-element ratio list-comp became
   `_tsi_signal_source()`: one numpy pass with identical elementwise IEEE
   operations (`(100.0 * pc) / ab` left-to-right) and the identical
   zero/NaN mask (`0.0` where `ds_abs == 0` or NaN, else the ratio).

## Differential evidence (the hard constraint)

`bot/tests/test_strategies/test_oleg_vectorized_parity.py` pins every
changed computation to a **verbatim copy of the pre-refactor code** and
compares within the task's 1e-9 float-tolerance contract:

| Level | What is compared | Coverage | Result |
|---|---|---|---|
| NW, per bar | `_nadaraya_watson_series(closes)[t]` vs legacy scalar on the same prefix — exactly what the growing-window backtest computed per bar | 8 data shapes × 3 bandwidths × 5 lookbacks, every bar | ≤ 1e-9 (bit-identical: measured max diff 0.0 over 4,320 bars) |
| NW, live shape | `series[-1]` vs the unchanged scalar oracle | 8 × 3 × 3 | ≤ 1e-9 |
| NW, edges | empty input; lookback ≤ 0; bandwidth = 0 (NaN parity in both); estimate within [min, max] of its window | — | pass |
| RCI, series | vectorized rank-correlation vs legacy `np.corrcoef` loop, NaN placement included | 8 shapes × 7 lengths (2…300) | max diff 8.5e-14 |
| RCI, at scale | 4,320-bar series vs legacy loop, L = 52 | — | max diff 8.5e-14 |
| RCI, anchors | strictly monotone windows saturate at exactly ±100 (both paths) | 3 lengths | pass |
| RCI, slice warrant | `_rci(s, L).iloc[-1]` == `_rci(s.iloc[-L:], L).iloc[-1]` — backs the call-site trailing-window slices | 8 shapes × 4 lengths | bit-identical |
| TSI, series | `_tsi_signal_source` vs legacy per-element loop, inputs built exactly as `on_candle` builds them (double `pta.ema`) | 8 datasets | ≤ 1e-9 |
| TSI, edges | all-zero denominators (even with NaN numerators), NaN warmup head, −0.0 denominators | — | pass |
| End-to-end | full `on_candle` signal streams over growing windows with the module patched back to the legacy oracles — actions, reasons, SL/TP brackets compared per bar | 2 configs × growing windows | identical streams |
| Wiring | every `_rci` call from `on_candle` receives exactly `length` bars | spy over a full stream | pass |

Data shapes in the zoo are adversarial for ranking/kernels: seeded random
walks, integer-quantized tie-heavy prices, two-level, constant, strictly
monotone up/down, sawtooth, single-spike.

Full suite on this branch: `cd bot && uv run pytest` — **731 passed,
1 skipped, 3 xfailed** (106.9 s).

## Performance

Synthetic 4,320-bar (180d of 1h) replay through the real
`hypertrade.backtest.runner.run_backtest` (no network/DB), and the real
CLI against live HyperLiquid data:

| Variant | Wall time (4,320 bars) | Trades |
|---|---|---|
| Pre-refactor legacy | ~100 min extrapolated from measured per-call cost; operator report: ">30 min hang" | — |
| Stage A — vectorized NW convolution + vectorized RCI, full-series calls (commit `f37f9a0`) | 171.5 s | 334 (167 round trips) |
| Stage B — + TSI vectorized, RCI computed on trailing windows | **36.4 s** | 334 (167 round trips) |

Both stages produce the identical trade stream on identical candles
(334 trades / 167 round trips / −4.53% / $150.27 fees) — a backtest-level
behavior-preservation datapoint on top of the per-bar tests.

Real CLI (this branch, live HyperLiquid ETH 1h data, `--days 180 --no-save`):

```
Backtest — oleg_aryukov on ETH 1h
  Period:        2026-03-15 → 2026-08-31  (170 days)
  Total return:  -3.63%
  Trades:        252 (126 round trips)
  Win rate:      34.9% (44W / 82L)
  Fees paid:     $113.35
```

Fetch of 4,321 real bars plus the full replay completed in ~90 s
end-to-end (compute portion ~36 s).

Reproduce with:

```
cd bot && uv run pytest tests/test_strategies/test_oleg_vectorized_parity.py -q
cd bot && uv run python scripts/bench_oleg_backtest.py
cd bot && uv run python -m hypertrade.backtest --strategy oleg_aryukov --days 180 --no-save
```

## Notes for reviewer

- The scalar `_nadaraya_watson` is intentionally kept: it is the
  differential oracle, and the series function is tested against it
  element-for-element.
- The NW series is intentionally NOT sliced to the trailing kernel at the
  call site (unlike RCI): the task asked for the rolling weighted
  convolution to be the production computation, and its total cost is
  ~0.1 s per 4,320-bar replay. Slicing would reduce the production path
  to the warmup loop and never exercise the convolution.
- Remaining per-call cost is pandas_ta wrapper overhead plus the
  recursive indicators (RSI / EMA / TSI smoothing) that genuinely need
  full history; further gains would need a different design (caching
  indicator state across bars) and are out of scope here.
- The legacy oracles in the test file are verbatim copies — do not
  "optimize" them; they are the ground truth this refactor was held to.
