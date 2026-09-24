# Pre-registration: strategy candidates and their two variants

> **Freeze date: the date this file was merged to master (PR #178).** It is the
> committer date of the squash commit that added the file:
> `git log --diff-filter=A --format='%cs %h' origin/master -- docs/preregistration.md`.
> From that date on, forward evidence for every row below starts to count.
>
> **Nothing in this file is ever edited after the freeze.** Every later
> change is a **new dated row** in the amendment log (§ 7). That covers a
> new variant, a corrected parameter, a changed live set and a typo fix. A
> row never changes the text above it. It supersedes that text from the
> row's own date. Rows are appended and never edited or deleted.

This is NU-10 point 1 of the next-level roadmap
(`docs/plans/next-level-roadmap.md`, § 4.2). It fixes what will be measured
before any forward data exists, so no candidate can be judged on a
definition chosen after its results were seen.

## 0. Rules

1. **Variant A is code; variant B is this text.** Variant A is the strategy
   file at the commit in § 1, run under the contract in § 2. Variant B is
   specified in § 4 as text. NA-2 writes the code for B and must implement
   the text exactly. If the code and the text disagree, the code is wrong.
   The only way to change B is a new row, and after anyone has seen a B
   result that row must say so.
2. **Forward window.** Evidence for a row counts from the first bar that
   opens at or after 00:00 UTC on the day after the row's date. No bar the
   author could have seen before the freeze counts as forward data.
3. **No results here.** This file holds no performance numbers. Results
   belong in the `validate` reports (NA-2) and the B1 decision (NA-14).
4. **An engine change can change variant A.** Say a change alters what
   `on_candle` receives (the window, closed bars only) or how exits
   execute. Then live no longer runs A as registered, and the change needs
   a row here in the same PR.

## 1. Code reference

| | |
|---|---|
| Commit | master `02ed3c15268d731243ab5b1cb1857274f6d5ff4c` (2026-09-23) |
| Runtime | Python 3.13 (`bot/Dockerfile`), pandas 3.0.2, numpy 2.2.6, pandas-ta 0.4.71b0 (`bot/uv.lock`) |
| Registry | 22 registered strategies (`bot/hypertrade/strategies/registry.py:58-81`): the 7 candidates, the 14 retired (§ 6) and `vvv_hedge`. `golden_cross` is not registered (`registry.py:82-86`). |

All paths below are relative to `bot/hypertrade/`. Every parameter was read
from the code at this commit, and each `meta/<name>.json` agrees with its
code on every parameter it lists.

## 2. Execution contract shared by all candidates (variant A as it runs)

- **Candle window.** Each tick fetches the bars inside
  `[now − 300 × timeframe, now]` (`fetch_candles` default `limit=300`,
  `data/feed.py:36-44`, called at `engine/runner.py:1214`). The last of
  those bars is still forming.
- **Closed bars only.** The runner drops the forming bar before calling
  the strategy (`engine/runner.py:1240`). So `on_candle` receives only
  closed bars, about 299 of them.
- **Recursive indicators depend on the window.** EMA, RSI, the RMA-smoothed
  ATR and the Kalman filter are seeded at the first bar of that window. The
  window is therefore part of both variants. The backtester at this commit
  feeds the full history instead (`backtest/runner.py:174`). A simulator
  that does the same is not evaluating variant A as it runs live, and its
  `validate` report must state which window it used.
- **Sizing.** Notional = `MAX_POSITION_SIZE_USD × leverage`
  (`strategies/base.py:45-49`; the code default is 1 000 at `config.py:121`,
  and the deployed value is operator config). A Redis override replaces a
  strategy's class leverage at boot (`main.py:271-276`). The exchange
  leverage per coin is the maximum over the running strategies on that coin
  (`main.py:278-281`).
- **Orders.** Every entry and exit is a market order that the engine sends
  after the bar has closed. No stop or take-profit order rests on the
  exchange. The runner never reads `Signal.stop_loss` or
  `Signal.take_profit` (`engine/signals.py:25-26`). Every stop below is
  checked by the strategy on the bars it receives.
- **Fill anchoring.** After an OPEN fills, `Strategy.on_filled`
  (`strategies/base.py:77-96`) sets `_entry_price` to the fill price. It
  does this only for a strategy that has that attribute, unless the
  strategy overrides the hook.
- **Coin and family gate.** With `allow_multi_coin=False`, only one
  strategy may hold a given coin, and only one strategy per `family` may
  hold a position across all coins (`engine/runner.py:1757-1795`).

## 3. Variant A: the seven candidates as they are today

"Last received bar" means the newest closed bar that `on_candle` receives.
"The bar before" is the one preceding it.

### 3.1 kalman_breakout

`strategies/kalman_breakout.py` · `meta/kalman_breakout.json` · Pine: `tv-source/kalman_breakout.pine`

| | |
|---|---|
| Symbol / timeframe | ETH / 1h (lines 61-62) |
| Direction | long and short |
| Leverage default | 1 (line 63) |
| Family | `kalman_breakout` (line 60) |
| Parameters | `process_noise_pos` 0.05, `process_noise_vel` 0.0001, `measurement_noise` 250.0, `band_lookback` 200, `band_multiplier` 2.6 (lines 65-69) |
| Fixed constants | filter state at the first bar of the frame: `x_p = close[0]`, `x_v = 0`, covariance = identity (lines 101-103); MAE = pandas rolling mean of `abs(close − kalman)` over `band_lookback` (line 162) |
| Warm-up | ≥ 205 bars received (line 148); ≥ 202 after the strip (line 153) |
| Stop-loss | **none**; no SL or TP |
| Second last-bar strip | **yes**, line 152: `closed = df.iloc[:-1]` |

- **Entry.** There are two signals, computed on the strip's output, so
  they compare the bar before the last received bar with the bar before
  that:
  - bull: close crosses above the upper band (line 178)
  - bear: close crosses below the lower band (line 179)

  OPEN_LONG fires on bull when not already long (lines 184-195). OPEN_SHORT
  fires on bear when not already short (lines 198-209).
- **Exit.** The strategy has no exit rule of its own. An opposite-side OPEN
  becomes close-then-open through the engine's flip detection
  (`engine/runner.py:1313-1317`, `1438-1444`).

### 3.2 keltner_breakout

`strategies/keltner_breakout.py` · `meta/keltner_breakout.json` · Pine: `tv-source/keltner_breakout.pine`

| | |
|---|---|
| Symbol / timeframe | ETH / 4h (lines 35-36) |
| Direction | long only |
| Leverage default | 1 (line 37) |
| Family | `keltner_channel` (line 34) |
| Parameters | `ema_len` 200, `kc_len` 20, `atr_len` 14, `kc_mult` 2.0, `sl_atr_mult` 4.0, `tp_pct` 0.20 (lines 39-44) |
| Indicators | `pta.ema(close, 200)`; `pta.atr(high, low, close, 14)` with pandas-ta's default RMA smoothing; KC mid = `pta.ema(close, 20)`, bands = mid ± 2.0 × ATR (lines 88-93) |
| Warm-up | ≥ 220 bars received (line 84) |
| Stop-loss | **yes**, see below |
| Second last-bar strip | **yes**, line 95: `closed = df.iloc[:-1]` |

- **Stop-loss and take-profit.** Both are fixed at entry:
  - SL = signal-bar close − 4.0 × the signal bar's ATR(14) (line 148)
  - TP = signal-bar close × 1.20 (line 149)

  Both are anchored to the signal-bar close, not to the fill. The strategy
  stores the price in `_entry`, and `on_filled` only updates
  `_entry_price`. After a restart with no saved SL, the SL is recomputed
  from the ATR at that time (lines 114-115).
- **Entry.** The strategy opens long when both conditions hold on the bar
  before the last received bar (`closed.iloc[-1]`, line 96):
  - close > EMA200 (line 143)
  - close > upper KC (line 144)

  The OPEN_LONG itself is at lines 145-158.
- **Exits.** They are checked in this order (lines 113-140):
  1. SL, when the **last received bar's** low ≤ SL (line 116)
  2. TP, when the last received bar's high ≥ TP (line 124)
  3. KC exit, when the close of the bar before the last received bar is
     below the lower KC (line 132)

  The SL and TP checks read `live = df.iloc[-1]` (line 98). The runner has
  already dropped the forming bar, so that is the last closed bar. The
  comment at line 97 calls it the in-progress bar, which is not what it is.

### 3.3 cdc_macd

`strategies/cdc_macd.py` · `meta/cdc_macd.json` · Pine: `tv-source/cdc_macd.pine`

| | |
|---|---|
| Symbol / timeframe | SOL / 1d (lines 30-31) |
| Direction | long only |
| Leverage default | 1 (line 32) |
| Family | `macd_zero_cross` (line 29) |
| Parameters | `ema_fast` 12, `ema_slow` 26 (lines 34-35), via `pta.ema` (lines 52-53) |
| Warm-up | ≥ 31 bars received (line 48) |
| Stop-loss | **none**; no SL or TP |
| Second last-bar strip | **yes**, line 55: `closed = df.iloc[:-1]` |

- **Entry.** EMA12 crosses above EMA26 (line 69), comparing the bar before
  the last received bar with the bar before that (lines 56-57). When flat,
  the strategy opens long (lines 72-82).
- **Exit.** EMA12 crosses below EMA26 (line 70). The strategy then closes
  the long (lines 84-94).

### 3.4 macd_zero

`strategies/macd_zero.py` · `meta/macd_zero.json` · Pine: `tv-source/macd_zero.pine`

| | |
|---|---|
| Symbol / timeframe | BTC / 1d (lines 33-34) |
| Direction | long only |
| Leverage default | 1 (line 35) |
| Family | `macd_zero_cross` (line 32), the same as cdc_macd |
| Parameters | `macd_fast` 12, `macd_slow` 26, `macd_signal` 9 (lines 37-39), via `pta.macd` (line 56). Only the MACD line is read (lines 60-64), so `macd_signal` affects no decision. |
| Warm-up | ≥ 41 bars received (line 52) |
| Stop-loss | **none**; no SL or TP |
| Second last-bar strip | **yes**, line 65: `closed = df.iloc[:-1]` |

- **Entry.** The MACD line crosses above 0: the previous value is ≤ 0 and
  the current value is > 0 (line 76). The two values come from the bar
  before the last received bar and the bar before that (lines 66-67). When
  flat, the strategy opens long (lines 79-86).
- **Exit.** The MACD line crosses below 0 (line 77). The strategy then
  closes the long (lines 88-95).

### 3.5 sma_rsi

`strategies/sma_rsi.py` · `meta/sma_rsi.json` · Pine: `tv-source/sma_rsi.pine`

| | |
|---|---|
| Symbol / timeframe | ETH / 1d (lines 33-34) |
| Direction | long only |
| Leverage default | 1 (line 35) |
| Family | `sma_rsi` (line 32) |
| Parameters | `sma_fast` 50, `sma_slow` 200, `rsi_length` 21, `rsi_smooth` 9, `rsi_threshold` 57.0 (lines 37-41). `rsi_ma` = `pta.sma(pta.rsi(close, 21), 9)` (lines 58-62). |
| Warm-up | ≥ 210 bars received (line 54) |
| Stop-loss | **none**; no SL or TP |
| Second last-bar strip | **no**; it reads the last received bar (line 64) |

- **Entry.** When flat, the strategy opens long if all three conditions
  hold (lines 75-85):
  - close > SMA50
  - close > SMA200
  - `rsi_ma` > 57

  This is a level condition, not a cross.
- **Exit.** When close < SMA50 and `rsi_ma` < 57, the strategy closes the
  long (lines 88-98).

### 3.6 ath_breakout

`strategies/ath_breakout.py` · `meta/ath_breakout.json` · a custom strategy, not a Pine port

| | |
|---|---|
| Symbol / timeframe | BTC / 1d (lines 49-50) |
| Direction | long only |
| Leverage default | **2** (line 60) |
| Family | `ath_breakout` (line 48) |
| Parameters | `lookback` 100 (line 64), `trail_pct` 0.35 (line 68) |
| Warm-up | ≥ 101 bars received (line 118) |
| Stop-loss | **trailing stop only**, see below |
| Second last-bar strip | **no**; it reads the last received bar (line 121) |

- **Trailing stop.** The strategy exits when a received bar's close is at
  or below peak × 0.65. The peak is the highest bar high since entry, and
  it starts at the signal bar's high (lines 160 and 129-134). There is no
  fixed initial stop.
- **Entry.** When flat, the strategy opens long if close > the maximum
  close of the 100 bars before the last received bar (lines 154-169).
- **Exit.** The peak is updated with each bar's high (lines 129-130), and
  the stop is peak × (1 − 0.35) (line 132). The strategy closes when close
  ≤ stop (lines 134-147).
- **Live caveat.** `state_json` is written only on OPEN
  (`engine/runner.py:1600-1629`), so a restart brings back the entry-time
  peak and loosens the trail until price makes a new high. This is an
  engine limitation, not part of the variant.

### 3.7 btc_mean_reversion

`strategies/btc_mean_reversion.py` · `meta/btc_mean_reversion.json` · Pine: `tv-source/btc_mean_reversion.pine`

| | |
|---|---|
| Symbol / timeframe | BTC / 15m (lines 39-40) |
| Direction | long and short |
| Leverage default | 1 (line 41) |
| Family | `btc_mean_reversion` (line 38) |
| Parameters | `ema_length` 200, `rsi_period` 14, `rsi_bull_level` 20.0, `rsi_bear_level` 65.0, `stoch_length` 14, `stoch_smooth_d` 3, `stoch_overbought` 75.0, `stoch_oversold` 25.0 (lines 44-51); `stop_loss_pct` 0.04, `take_profit_pct` 0.06 (lines 54-55) |
| Fixed constants | the long filter's EMA factor 0.9 is hard-coded (line 182). %K is raw fast %K from `pta.stoch(k=14, d=3, smooth_k=1)` (lines 159-162), so `stoch_smooth_d` only shapes the unused %D line. |
| Warm-up | ≥ 205 bars received (line 106) |
| Stop-loss | **yes**, see below |
| Second last-bar strip | **no**; it reads the last received bar (lines 109 and 170) |

- **Stop-loss and take-profit.** Both are fixed, at 4 % (SL) and 6 % (TP)
  from entry. They are first set from the signal-bar close (lines 193-194
  and 210-211), then re-anchored to the fill price by `on_filled` (lines
  64-66). They trigger when the last received bar's low or high touches the
  level, and SL is checked before TP.
- **Entry.** The long condition is checked first:
  - Long, when RSI < 20, %K < 25 and close > 0.9 × EMA200 (lines 179-183).
    The OPEN_LONG is at lines 190-205.
  - Short, when RSI > 65, %K > 75 and close < EMA200 (lines 184-188). The
    OPEN_SHORT is at lines 207-222.
- **Exit.** The exits are at lines 115-152:
  - long SL when low ≤ SL (line 119)
  - long TP when high ≥ TP (line 127)
  - short SL when high ≥ SL (line 136)
  - short TP when low ≤ TP (line 144)

## 4. Variant B: specification (no code)

B changes two things, and only for some candidates. Everything not named
here, including parameters, warm-up guards, conditions and leverage, is
identical to A.

**The signal bar.** Under B, every rule is evaluated on the last received
bar, which is the newest closed bar. "The signal bar" is the bar an OPEN
was decided on.

### 4.1 (i) Remove the second last-bar strip

This applies to kalman_breakout (line 152), keltner_breakout (line 95),
cdc_macd (line 55) and macd_zero (line 65). All four lines were checked at
the commit in § 1.

The runner already passes only closed bars (`engine/runner.py:1240`), so
each of these lines drops a closed bar. Under B:

- A value that A reads from `closed.iloc[-1]` is read from the last
  received bar.
- A value that A reads from `closed.iloc[-2]` is read from the bar before
  it.

Every entry and every signal-based exit of these four therefore happens one
bar earlier: 1h for kalman_breakout, 4h for keltner_breakout, and 1d for
cdc_macd and macd_zero.

For keltner_breakout, (i) moves three things:

- the entry conditions
- the ATR used for the SL at entry
- the KC exit

The SL and TP checks already use the last received bar and do not change.
Two things stay as they are in A:

- The SL remains locked at entry.
- Pine's per-bar SL recomputation is not part of B. Roadmap NA-3 allows
  that only as a new row.

### 4.2 (ii) Catastrophe stop

This applies to kalman_breakout, cdc_macd, macd_zero and sma_rsi, the four
candidates with no stop-loss.

- **Entry price E.** E is the fill price of the opening order. Live, it is
  the price the engine passes to `on_filled`. In the simulator, it is the
  modelled entry fill.
- **ATR at entry.** ATR(14) with Wilder (RMA) smoothing, computed exactly
  as `pandas_ta.atr(high, low, close, length=14)` with its default
  `mamode="rma"`, which is the call `keltner_breakout.py:89` makes. It is
  computed on the frame the strategy received, and the value is taken on
  the signal bar. It is fixed for the life of the position and never
  recomputed. If it is NaN, only the 8 % term applies.
- **Distance.** D = max(3 × ATR at entry, 0.08 × E).
- **Stop level.** L = E − D for a long. For a short, which only
  kalman_breakout can hold, L = E + D.
- **Evaluation on closed bars.** L is checked on every closed bar after the
  signal bar while the position is open. A long triggers when that bar's
  **close** ≤ L, and a short when its close ≥ L. Intrabar highs and lows
  are not used.
- **Execution.** The stop closes the whole position through the same path
  as any other strategy exit. Live, that is a market close after the bar
  closes. In the simulator, it is the same fill model as other exits. No
  order rests on the exchange; that is SE-1, a separate thing.
- **Precedence on a bar.** A's own rule is evaluated first, with (i)
  applied where it applies. If A emits a signal that ends the position,
  that signal is emitted and the stop is not checked on that bar. Such a
  signal is either a CLOSE or, for kalman_breakout, an opposite-side OPEN
  that the engine turns into close-then-open. Otherwise the stop is
  checked. B is therefore A plus extra exits, only on bars where A would
  hold.
- **After a stop.** The strategy is flat, and A's entry rule applies
  unchanged from the next closed bar, with no cooldown and no re-arm
  condition.
  - kalman_breakout, cdc_macd and macd_zero enter on a cross, so they need
    a fresh cross before they re-enter.
  - sma_rsi enters on a level condition (§ 3.5), so it can re-enter on the
    next closed bar if all three conditions still hold.
- **Persistence (matters for NA-3 live, not for the simulator).** L is
  part of the exported state and is restored verbatim after a restart. A
  position that has no saved L uses D = 0.08 × E.

### 4.3 What B is, per candidate

| Candidate | (i) strip removed | (ii) catastrophe stop | Variant B |
|---|---|---|---|
| kalman_breakout | yes | yes | A + (i) + (ii) |
| keltner_breakout | yes | no, it has an SL | A + (i) |
| cdc_macd | yes | yes | A + (i) + (ii) |
| macd_zero | yes | yes | A + (i) + (ii) |
| sma_rsi | no, it has no second strip | yes | A + (ii) |
| ath_breakout | no | no, it has a trailing stop | **B = A** |
| btc_mean_reversion | no | no, it has an SL | **B = A** |

There is no variant with only one of (i) and (ii). Adding one takes a new
row.

## 5. Live set (decision 5.11, approved 2026-09-24)

There is one candidate per coin in live-paper and testnet. The other
candidates are validated offline only, with `validate` and replay.

| Coin | Runs live (variant A) | Validated offline only |
|---|---|---|
| ETH | kalman_breakout | keltner_breakout, sma_rsi |
| SOL | cdc_macd | none |
| BTC | btc_mean_reversion | macd_zero, ath_breakout |

- Live runs variant A. Variant B exists only in the NA-2 simulator until
  NA-3 puts an approved variant live behind a flag, which happens after B1.
- No candidate trades on mainnet. Decision 5.1 says mainnet is not funded
  before B1.
- macd_zero cannot run live next to cdc_macd, because they share the family
  `macd_zero_cross` and the family gate lets only one of them hold a
  position (`engine/runner.py:1786-1795`).
- Live is a fidelity check (decisions 5.6 and 5.11). Evidence of edge, for
  all seven candidates, comes from `validate` and replay.
- The live set is applied in Redis when the strategies are flat (NU-10
  point 3). This file records the decision, not the Redis state.

## 6. Retired strategies (decision 5.2, approved 2026-09-24)

The operator approved retiring these 14 on 2026-09-24:

daily_long_0830, moon_phases, hash_momentum, oleg_aryukov,
qullamagi_breakout, volatility_breakout, hash_supertrend, supertrend,
pivot_supertrend, ema_crossover, penguin_volatility, bb_rsi_scalper,
rsi_momentum, bb_short.

- They are not pre-registered, so no forward evidence is collected for
  them.
- They are disabled in paper and testnet once flat (NU-10 point 2). Their
  code stays in the registry until NA-4, and deregistering them requires
  asking the operator first (CLAUDE.md § 7).
- Bringing one back requires a new row here and the promotion gate (NA-2).
- `vvv_hedge` is neither a candidate nor one of the 14. Decision 5.2 takes
  it out of the tradeable registry (NA-4).

## 7. Amendment log

Append new rows at the bottom. Never edit or delete a row. Each row names
what it supersedes, and its forward window follows rule 2 in § 0.

| # | Date (UTC) | Change | Applies to | Reason | PR |
|---|---|---|---|---|---|
| 1 | Freeze date (merge date of #178) | Initial registration: variant A at `02ed3c1` and the variant B specification for the seven candidates; live set per 5.11; 14 retired per 5.2 | all | NU-10 point 1 | #178 |
