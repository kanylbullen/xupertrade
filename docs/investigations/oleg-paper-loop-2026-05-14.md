# `oleg_aryukov` paper-bot instant-stop-out loop — 2026-05-14

> **TL;DR.** The strategy evaluates trail-stop and SL/TP-hit checks
> against bar data that **pre-dates the position's entry**. Combined with
> a stable vote tally across all minute-ticks within the same hourly bar,
> this produces a loop where every tick re-enters and every next tick
> instantly stops out.
>
> **PR #126 (`reset_state` consolidation) was correct.** **PR #127 (move
> trail+exit checks to `iloc[-2]`) is wrong** — it doesn't fix the bug,
> and it introduces a real off-by-one (the strategy now ignores the most
> recent CLOSED bar entirely, because `runner.py:608` already strips the
> live partial bar before invoking `on_candle`). PR #127 should be
> reverted as part of the real fix.

---

## Symptom

Paper bot, 2026-05-14, 15:01:39–15:12:46 UTC. ETH 1h. 10 trades in 11
minutes. Every CLOSE row's reason is bit-identical:

```
337  sell  2287.7  Short ensemble: 3/3+ sell votes, ... SL=$2,331.82 TP=$2,194.66
338  buy   2284.4  SL hit at $2,267.96 (entry $2,286.10, high $2,287.80)
339  sell  2283.7  Short ensemble: 3/3+ sell votes, ... SL=$2,331.82 TP=$2,194.66
340  buy   2284.9  SL hit at $2,267.96 (entry $2,286.10, high $2,287.80)
341  sell  2282.2  Short ensemble: 3/3+ sell votes, ... SL=$2,331.82 TP=$2,194.66
342  buy   2280.9  SL hit at $2,267.96 (entry $2,286.10, high $2,287.80)
…  (5 more cycles)
```

Notable: the OPEN-side `SL=$2,331.82` is computed from
`entry × (1 + 2%)` ⇒ entry of `$2,286.10`. But the OPEN-side fill
price (the actual sell price reported in `trades.price`) is varying
(2287.7 / 2283.7 / 2282.2 / …). So **the strategy's `_entry_price` is
never the actual fill** — it's `df.iloc[-1].close` of the closed-bar
slice the runner passes in.

Within an 11-min window the closed bars don't change, so this stays
constant — the same is true for the CLOSE-side `entry` and `high`
labels.

---

## Hypothesis 1 — *partial-bar low/high* (PR #127, **wrong**)

PR #127 hypothesised that the strategy was reading the **live, in-progress
1h bar** at `df.iloc[-1]` — whose `high`/`low` already span every tick
since the bar opened — and that this was what ratcheted the trailing
stop to within ~1% of the entry.

**Why this is wrong:** `bot/hypertrade/engine/runner.py:608` already
strips the live partial bar before calling `on_candle`:

```python
# runner.py line 606–609
closed_candles = candles.iloc[:-1] if len(candles) > 1 else candles
signal = await strategy.on_candle(closed_candles)
```

So inside any strategy `df.iloc[-1]` is **already** the latest CLOSED bar.
PR #127's switch to `df.iloc[-2]` therefore creates a real off-by-one:
the strategy now ignores the most-recent closed bar and evaluates trail/exit
on the *second*-most-recent closed bar. That doesn't fix the loop and
masks the real cause.

PR #127 was authored without knowledge of `runner.py:608` (PR #128 — the
sister fix for `vol_breakout` / `supertrend` — was closed for the same
reason after Copilot pointed out the live-bar stripping).

---

## Hypothesis 2 — *exit checks consult a pre-entry bar* (real cause)

`bot/hypertrade/strategies/oleg_aryukov.py:240–298` (post-PR #127):

```python
if self._position_side is not None and self._entry_price is not None:
    if len(df) < 2:
        return None
    closed = df.iloc[-2]                 # ← bar that pre-dates the entry
    high = float(closed["high"])
    low  = float(closed["low"])

    if self.use_trailing and self._trail_extreme is not None:
        trail_dist = self._entry_price * self.trailing_percent / 100.0
        # … updates _trail_extreme from `low` (short) or `high` (long)
        # … then sets _stop_loss = trail_extreme +/- trail_dist
    sl = self._stop_loss
    if self._position_side == "short" and sl is not None and high >= sl:
        # CLOSE_SHORT
```

**The fundamental flaw is independent of `iloc[-1]` vs `iloc[-2]`:**
the trailing-extreme update and the SL-hit comparison both consult bars
that closed **before** the position was opened. There is no "ignore
pre-entry bars" guard and no "first-tick-after-open skip" logic.

### Worked example (matches the prod numbers)

Window: 15:01–15:12, an hour into 1h bar `15:00–16:00`. Closed bars:

| `df.iloc` index | bar           | `close` | `high`  | `low`   |
|:---------------:|:-------------:|:-------:|:-------:|:-------:|
| `-1`            | 14:00–15:00   | 2286.10 | …       | …       |
| `-2`            | 13:00–14:00   | …       | 2287.80 | 2245.10 |

1. Tick at 15:01:39 — strategy is FLAT. Indicator votes ≥ 3 SHORT,
   trend filter passes. Open path:
   ```python
   close = df.iloc[-1].close            # 2286.10
   self._entry_price   = 2286.10
   self._stop_loss     = 2286.10 × 1.02 = 2331.82   # static %SL
   self._trail_extreme = 2286.10
   ```
   Reason string: `"… SL=$2,331.82 …"` ✓

2. Tick at 15:02:39 — same df (no new closed bar yet). Strategy is SHORT.
   Branch enters the *manage open position* block:
   ```python
   closed = df.iloc[-2]                 # bar 13:00–14:00
   high = 2287.80
   low  = 2245.10
   trail_dist  = 2286.10 × 0.01 = 22.86
   # short: extreme tracks low
   if low (2245.10) < trail_extreme (2286.10):
       trail_extreme = 2245.10
   trail_stop = trail_extreme + trail_dist = 2245.10 + 22.86 = 2267.96
   _stop_loss = min(2331.82, 2267.96)   = 2267.96    ← ratchets DOWN
   # short SL hit:  high (2287.80) >= sl (2267.96)  → True
   ```
   `CLOSE_SHORT` fires with reason
   `"SL hit at $2,267.96 (entry $2,286.10, high $2,287.80)"` ✓ —
   bit-identical to the prod log.

3. `reset_state()` clears strategy state. DB closes the row. Engine
   advances.

4. Tick at 15:03:39 — same df. Strategy is FLAT. Indicators **STILL**
   show 3+ short votes (the closed bars haven't moved), so OPEN_SHORT
   fires again with the same `_entry_price = 2286.10`.

5. Loop repeats every minute until either the bar `15:00–16:00` closes
   (and the new closed-bar set produces different votes / different
   trail extremes) or the strategy is disabled by the operator. In
   prod, the loop ran ~10 trades over 11 minutes before being disabled
   in Redis.

### Why neither pre-PR #127 nor PR #127 prevent this

* **Pre-PR #127** the strategy used `df.iloc[-1]` for trail/exit. After
  runner stripping, that's bar `14:00–15:00`. If `14:00–15:00`'s low
  fell below entry (which it generally would for any non-trivial bar),
  the trail still ratcheted on the very first tick after open. Same
  loop, reading a different pre-entry bar.
* **PR #127** moved to `df.iloc[-2]` (= `13:00–14:00`). Same loop;
  reads a bar even further from the entry. Plus introduces an off-by-one
  by ignoring `df.iloc[-1]` entirely (which IS the latest closed bar
  the runner intended for the strategy to see).

### Other strategy-state observations along the way

* `_entry_price` is assigned `close = df.iloc[-1].close` of the
  runner-stripped frame — i.e. the close of the *last fully-closed*
  candle, NOT the actual exchange fill price. That's why the
  open-signal's `SL=$2,331.82` (= 2.0% × `2286.10`) doesn't bracket
  the *actual* sell-fill (2287.7) cleanly. This is a labelling /
  realism issue; on its own it would not produce the loop. The right
  fix is for the engine, after `_execute_signal` fills, to update
  the strategy's `_entry_price` (and `_stop_loss` / `_take_profit`)
  to the actual `filled_price`. See *Recommended fix* below.
* `reset_state()` (PR #126) IS correct and necessary — without it,
  stale `_stop_loss` from a previous trade would have leaked across
  the close. PR #126 should NOT be reverted; this investigation
  found no bug in that consolidation.
* `restore_from_json` and `restore_state` correctly initialise
  `_trail_extreme = entry_price`, so a restart inside an in-position
  window doesn't reload a stale trail extreme.

---

## Reproduction

A failing test is included in `bot/tests/test_strategies/test_oleg_paper_loop_repro.py`,
marked `xfail(strict=True)` so CI documents the bug without breaking
the build. Scenario:

1. Build a `df` with a long flat history and a few trailing
   "closed-bar" rows whose `iloc[-2]` carries
   `low = entry × 0.982` and `high = entry × 1.0007` — the prod
   shape (low well below entry, high slightly above).
2. Manually open a fresh short by calling `restore_state("short", entry)`
   (matches the on_candle open path's effect on internal state).
3. Call `on_candle(df)`. Assert that the result is `None` (the open
   should NOT instantly stop out).
4. Today's behaviour: the very first tick after entry returns
   `CLOSE_SHORT` with the bug-reason `SL hit at $X (entry $Y, high $Z)`
   — the test fails as expected, marking the bug as reproduced.

The xfail is marked `strict=True` so when the bug is fixed and the test
unexpectedly passes, CI will turn red and force us to flip it to a
regular passing test in the same PR that lands the fix.

---

## Recommended fix (NOT YET IMPLEMENTED)

**Three changes, smallest first.** Land each as its own PR with a
focussed test.

### A. Revert PR #127 (`iloc[-2]` → `iloc[-1]`)

PR #127 doesn't fix the loop and creates a real off-by-one (skips the
most recent closed bar). Inline the revert and update the two
regression tests it added (which assert the off-by-one behaviour) to
test the closed-bar semantics correctly. Specifically:

* `test_partial_bar_low_does_not_stop_out_long` — keep the *intent*
  ("a stale partial bar must not drive an exit") but recognise that
  the runner already strips the live bar, so the regression target
  becomes "exit checks must not consult bars from before entry"
  (= scenario tested by the new repro).
* `test_partial_bar_high_does_not_stop_out_short` — same.
* `test_sl_exit_closes_long` (the SL-hit test PR #127 moved to
  `iloc[-2]`) — keep on `iloc[-1]` (= latest closed bar from the
  runner's stripping).

### B. Track the bar at which the position was opened

Add `self._entry_bar_ts: pd.Timestamp | None = None` (or bar index).
Set in the open path; null in `reset_state()` and `__init__`.

In the manage-open block:

```python
if self._entry_bar_ts is not None:
    closed_bars_since_entry = df[df["timestamp"] > self._entry_bar_ts]
    if len(closed_bars_since_entry) == 0:
        return None  # no new closed bar since open — nothing to evaluate
    last_closed = closed_bars_since_entry.iloc[-1]
    high = float(last_closed["high"])
    low  = float(last_closed["low"])
```

This guarantees the trail-extreme and SL-hit checks see only bars
that closed **after** the position opened. On the very first tick after
open (no new closed bar yet), `on_candle` is a no-op for the manage
branch — exactly what we want.

### C. Adopt the exchange's actual fill price as `_entry_price`

The engine knows `filled_price` after `place_order`. Add a hook (e.g.
`Strategy.on_filled(side, fill_price)`) that the runner calls in
`_execute_signal` immediately after a successful OPEN fill, and have
`oleg_aryukov` (and any other percentage-SL strategy) update
`_entry_price`, `_stop_loss`, `_take_profit`, `_trail_extreme` from
the real fill price. Without this, the SL/TP brackets are anchored to
the *prior bar's close* rather than the actual entry — fine for
backtesting against synthetic data, lossy in live trading where the
spread moves between signal-time and fill-time.

This is a broader change (touches the `Strategy` base class and the
runner) so it should be a separate PR after A and B land. It also has
implications for backtesting (do we apply the same fill-realism
correction?) which deserve discussion.

### Stop-gap: keep `oleg_aryukov` disabled

Until A + B land, leave the strategy disabled in Redis on all three
modes. The bug is deterministic on every 1h ETH window where the most
recent closed bars happen to be both (a) trend-aligned in vote
direction and (b) wide enough to ratchet the trail past the static
SL on the very first tick — common enough that it'll re-trigger.

---

## Recommendation re PR #127

**Revert.** PR #127 is a confirmed off-by-one masquerading as a fix. It
makes the strategy ignore the most recent closed bar (which the runner
specifically passes in for that purpose). It happened not to cause harm
yet only because `oleg_aryukov` is currently disabled. Re-enabling
without the revert + Fix B would still loop, and additionally would
silently use stale 2-bar-old data for trail/exit decisions in any
non-loop scenario.

**PR #126 stays as-is.** The `reset_state` consolidation is correct and
defensible on its own merits.

---

## File references

| File / line                                                | Purpose                                                                                                              |
|------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `bot/hypertrade/strategies/oleg_aryukov.py:240–298`        | Manage-open block — the trail/SL-hit logic that consults pre-entry bars (root cause)                                 |
| `bot/hypertrade/strategies/oleg_aryukov.py:434–468`        | Open-path: sets `_entry_price = df.iloc[-1].close` (not actual fill); same code on long and short                    |
| `bot/hypertrade/strategies/oleg_aryukov.py:204–219`        | `reset_state()` from PR #126 — correct                                                                               |
| `bot/hypertrade/engine/runner.py:606–609`                  | Strips live partial bar before calling `on_candle` (the fact PR #127 didn't know about)                              |
| `bot/hypertrade/engine/runner.py:927–937`                  | Engine has `filled_price` after the order fills — hook target for Fix C                                              |
| `bot/tests/test_strategies/test_oleg_paper_loop_repro.py`  | xfail(strict) repro test for the loop                                                                                |

---

*Investigation completed 2026-05-15.*
