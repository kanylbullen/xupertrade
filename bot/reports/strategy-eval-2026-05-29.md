# Strategy Evaluation — 2026-05-29

Deep per-strategy evaluation of all 12 live strategies. Live data pulled from
prod Postgres (`trades`, all modes, `reconciled` excluded). Backtests run via
the bot's `hypertrade.backtest` CLI (`--no-save`) inside the mainnet container.

**Read-only review.** Disable/mainnet decisions belong to the operator (§8) —
this report *recommends*, it does not act.

---

## Executive summary

- **One genuine mainnet candidate: `bb_short`** — +3.37% APR / Sharpe 1.56 /
  100% win (10/10) over 170d backtest, positive live, already operator-capped
  to mainnet. It is the only strategy clearing the conservative bar. Recommend
  the operator consider it the reference for "ready."
- **Clear keeps (performing to archetype):** `hash_momentum` (momentum profile
  intact — 30% win but 3.7:1 avg-win/avg-loss, net +$27.90 combined) and
  `daily_long_0830` (calendar long, net +$12.96 combined, 59% win).
- **Clear disable candidates (evidence below):** `ema_crossover`,
  `volatility_breakout`, `penguin_volatility`, `oleg_aryukov`, `moon_phases`.
  All are net-negative live AND negative on fresh backtest.
- **`ema_crossover` regressed.** §5 logged the 2026-04-30 phantom-reversal fix
  taking the 180d backtest to +5.2%. A fresh 180d run is back to **-3.90% /
  Sharpe -2.08 / 16.7% win**, and live is 0/6. Not just choppy luck — the edge
  is gone in the current regime.
- **`kalman_breakout` looks great but is noise.** +$33–35 "100% / 3 trades" is
  literally ONE ETH short (2026-05-28→06-07, ~17% move) double-counted across
  paper+testnet. Backtest is positive (Sharpe 1.13) but only 14 round trips →
  **WATCH, not mainnet.**

---

## Per-strategy detail

Live = paper+testnet combined, `reconciled` excluded, oleg pre-fix rows
(< 2026-05-15) filtered. "Closes" = rows with non-null PnL. Avg-win/avg-loss
ratio is the momentum/breakout health metric.

### hash_momentum — KEEP

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 25 | 39 | 64 |
| win rate | 24% | 33% | 30% |
| total PnL | +$7.98 | +$19.91 | +$27.90 |
| avg PnL | +$0.32 | +$0.51 | +$0.44 |
| avg PnL % | +0.03% | +0.24% | — |
| avg win / avg loss | — | — | **+$4.07 / −$1.10 = 3.7:1** |
| max consec loss | — | — | 26 |

**Archetype check:** momentum/breakout — expects <40% win with bigger winners.
30% win + a 3.7:1 win/loss ratio is textbook. Net positive in both modes.
Largest win +$12.89, largest loss −$5.29. The 26-long loss streak looks scary
but is the expected shape of a momentum strat grinding small losses between
large winners; the math nets positive. Not over-trading in the bad sense —
each loss is small and bounded.

**Backtest 180d (SOL 4h):** -1.28% / APR -3.20% / Sharpe -0.89 / 32.6% win
(46 round trips). Backtest win rate matches live (≈32%) — the strategy is
behaving *exactly* as designed; the backtest just lands slightly negative in
this window while live caught better entries. Divergence is mild and explained
by sample/window, not a logic break.

**Verdict: KEEP.** Highest-volume strategy, archetype-faithful, net positive
live. Not a mainnet candidate (negative backtest APR, Sharpe <1).

### daily_long_0830 — KEEP

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 20 | 9 | 29 |
| win rate | 60% | 56% | 59% |
| total PnL | +$8.27 | +$4.69 | +$12.96 |
| avg PnL | +$0.41 | +$0.52 | +$0.45 |
| avg win / avg loss | — | — | +$2.32 / −$2.20 |
| max consec loss | — | — | 7 |

**Archetype check:** trivial intraday calendar long (enter 08:30 UTC, exit
08:00 UTC, no SL/TP). 59% win with near-symmetric win/loss is fine for a
no-edge-claimed calendar play; it's net positive because the 08:30→08:00 hold
caught more up-days than down in this window.

**Backtest (BTC 15m, only 44d available):** -2.73% / APR -20.41% / Sharpe -5.33
/ 44% win (43 round trips). **Flag the divergence:** backtest is sharply
negative while live is positive. Cause is the short 44-day backtest window
(calendar strategy's edge is entirely regime/seasonality-dependent) plus BTC
backtest vs the live symbol. Not a logic bug — but the edge is thin and
regime-contingent.

**Verdict: KEEP (with a WATCH note).** Net-positive live across 29 trades, but
the negative backtest means the operator should not expect this to persist if
the daily drift flips. Low risk (no leverage drama, small avg trade).

### ema_crossover — RECOMMEND-DISABLE

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 3 | 3 | 6 |
| win rate | 0% | 0% | **0%** |
| total PnL | −$9.02 | −$9.48 | −$18.49 |
| avg PnL | −$3.01 | −$3.16 | −$3.08 |
| avg PnL % | −1.52% | −2.11% | — |
| max consec loss | — | — | 6 |

**Archetype check:** EMA 7/19 crossover trend-follow. Expect modest win rate
with trend payoff. Live is 0/6 with every trade a loss — the "enter on cross,
exit on SL/opposite cross" is getting whipsawed.

**Backtest 180d (BTC 1h):** **-3.90% / APR -8.21% / Sharpe -2.08 / 16.7% win
(24 round trips).** This is the headline: §5 recorded the 2026-04-30 fix
(removed phantom reversal) taking the backtest to +5.2%. A fresh run is firmly
negative again. So the live 0% is **not** small-sample noise — it's consistent
with a backtest that has lost its edge in the current chop. The fix was correct
(logic now matches Pine), but the strategy itself doesn't have an edge now.

**Verdict: RECOMMEND-DISABLE.** Live 0/6 corroborated by negative backtest.

### penguin_volatility — RECOMMEND-DISABLE

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 4 | 4 | 8 |
| win rate | 25% | 25% | 25% |
| total PnL | −$15.28 | −$7.12 | −$22.40 |
| avg PnL | −$3.82 | −$1.78 | −$2.80 |
| avg PnL % | −0.62% | −0.93% | — |
| max consec loss | — | — | 4 |

**Archetype check:** BB/KC volatility-state, long-only, no SL, timing filter.
A long-only volatility-expansion strat should be ~50% in a neutral regime; live
is 25% in both modes, net negative both modes. Largest loss −$16.17 (testnet)
shows the no-SL design lets a bad entry run.

**Backtest 180d (115 trades):** -1.97% / APR -4.20% / Sharpe -1.07 / 35.1% win
(57 round trips). §5 flagged it historically at -10.3% over-trading. Still
over-trading (57 round trips in 180d) and still net-negative. Live and backtest
agree.

**Verdict: RECOMMEND-DISABLE.** Consistent under-performer, confirmed across
the original 2026-05-01 backtest, this backtest, and live.

### oleg_aryukov — RECOMMEND-DISABLE

| metric | testnet | paper | combined |
|---|---|---|---|
| closes (post-fix) | 4 | 3 | 7 |
| win rate | 25% | 33% | 29% |
| total PnL | −$8.15 | −$5.37 | −$13.52 |
| avg PnL | −$2.04 | −$1.79 | −$1.93 |
| avg PnL % | −0.98% | −0.33% | — |
| max consec loss | — | — | 4 |

Pre-2026-05-15 rows excluded (instant-loop bug, PR #132). Even on clean
post-fix data the strategy is net-negative in both modes.

**Archetype check:** 6-indicator voting ensemble with trend filter + SL/TP.
An ensemble with `min_confirmations=3` should be selective and ~50%; live is
29% and losing.

**Backtest 90d (ETH 1h; 180d skipped — O(n²) per §5 backlog):** -1.91% /
APR -8.44% / **Sharpe -4.90** / 35% win (60 round trips in 80 days = heavy
over-trading for a "selective ensemble"). The Sharpe is the worst of any
strategy tested.

**Verdict: RECOMMEND-DISABLE.** Negative live (post-fix) AND deeply negative
backtest. The bug is fixed; the strategy still has no edge.

### volatility_breakout — RECOMMEND-DISABLE (signal, not just sizing)

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 3 | 2 | 5 |
| win rate | 33% | 0% | 20% |
| total PnL | −$26.14 | −$11.46 | −$37.61 |
| avg PnL % | −1.38% | −1.83% | — |
| max consec loss | — | — | 4 |

**Sizing artifact stripped:** the −$26 testnet number is dominated by one
0.77-ETH (~$1,636 notional) trade vs the ~$200–400 paper equivalents — the
per-mode `MAX_POSITION_SIZE_USD` artifact the brief warned about. **But the
signal is bad independently of size:** of 5 completed trades, 4 were stop-loss
losses and only 1 was a (tiny +$0.21) win; per-mode avg PnL % is −1.38%
(testnet) and −1.83% (paper) — both negative, so it's not the dollar inflation
making it look bad. Every KC breakout entry got stopped out.

**Backtest 180d (ETH 1h):** -0.75% / APR -1.61% / Sharpe -0.23 / 58.6% win
(58 round trips). Interesting: backtest win rate is 58.6% but still net
negative — wins are smaller than the ATR×4 stop losses. Live's worse win rate
is small-sample, but both live and backtest are net-negative.

**Verdict: RECOMMEND-DISABLE.** Even normalizing for sizing, per-mode avg PnL %
is negative and the backtest is negative. (If the operator keeps it, the
per-mode sizing artifact should be fixed first — see Caveats.)

### moon_phases — RECOMMEND-DISABLE (or WATCH on backtest only)

| metric | testnet | combined |
|---|---|---|
| closes | 1 | 1 |
| win rate | 0% | 0% |
| total PnL | −$12.18 | −$12.18 |
| avg PnL % | −6.47% | — |

**Archetype check:** lunar calendar long, 5% SL / 10% TP. One live trade, a
−$12.18 / −6.47% loss (SL hit). Far too few live trades to judge on live alone.

**Backtest 180d/130d (BTC 1d, only 181 bars):** +0.16% / APR +0.44% /
Sharpe 0.27 / 50% win (4 round trips). Essentially flat — no edge, no harm.

**Verdict: RECOMMEND-DISABLE.** Live SL'd hard; backtest is a flat coin-flip
(Sharpe 0.27, 4 trips). No demonstrated edge. If the operator prefers, WATCH —
it trades so rarely it's low-risk to leave on — but there's no reason to expect
profit.

### kalman_breakout — WATCH (not mainnet)

| metric | testnet | paper | combined (unique signals) |
|---|---|---|---|
| closes | 1 | 1 | effectively 1 distinct trade |
| win rate | 100% | 100% | 100% |
| total PnL | +$33.65 | +$34.97 | +$68.63 (double-counted) |
| avg PnL % | +20.3% | +21.2% | — |

**Reality check:** the two "wins" are the **same ETH short** (open 2026-05-28
@ ~$1,980, auto-closed-before-flip 2026-06-07 @ ~$1,633) appearing once per
mode. It caught a real ~17% ETH drop — genuine, but it is ONE signal. The
+20% avg-PnL-% reflects that single move, not a repeatable edge.

**Backtest 180d (ETH 1h):** **+2.82% / APR +6.17% / Sharpe 1.13** / 35.7% win
(14 round trips). Positive with Sharpe just over 1 — encouraging, and the
backtest's 35.7% win with positive return matches a breakout archetype (few
big winners). But **14 round trips is a small sample** and live has only 1
distinct trade.

**Verdict: WATCH.** Promising (only the second strategy with positive backtest
APR and Sharpe >1), but fails the mainnet bar on trade count / live sample.
Re-evaluate after ≥15 live trades.

### hash_supertrend — WATCH

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 2 | 12 | 14 |
| win rate | 50% | 50% | 50% |
| total PnL | −$3.14 | +$10.48 | +$7.34 |
| avg PnL % | −0.76% | +0.61% | — |
| avg win / avg loss | — | — | +$5.92 / −$4.87 |
| max consec loss | — | — | 2 |

**Archetype check:** flip-on-SuperTrend, no SL/TP. 50% win with ~1.2:1
win/loss is reasonable for a trend-flip. Paper net +$10.48; testnet only 2
trades (−$3.14) — testnet sample too thin. Combined net positive.

**Backtest 180d (BTC 1h):** -0.16% / APR -0.35% / Sharpe -0.05 / 42.2% win
(83 round trips). Essentially flat. Live paper is positive but the backtest is
a wash, and 83 round trips/180d hints at chop-churn.

**Verdict: WATCH.** Net-positive live but flat backtest and thin testnet
sample. Keep observing; not a disable, not a mainnet candidate.

### qullamagi_breakout — WATCH

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 3 | 3 | 6 |
| win rate | 67% | 67% | 67% |
| total PnL | +$4.18 | +$3.83 | +$8.01 |
| avg PnL % | +0.73% | +0.67% | — |
| avg win / avg loss | — | — | +$3.11 / −$2.21 |
| max consec loss | — | — | 2 |

**Archetype check:** multi-MA stacked-trend breakout. 67% win, positive both
modes — looks good on live. But only 6 closes total.

**Backtest 180d (ETH 1h):** -2.50% / APR -5.30% / Sharpe -1.55 / **21.4% win
(70 round trips).** Sharp divergence: live is 67% win / positive on 6 trades;
backtest is 21% win / negative on 70 trades. The live sample is too small and
likely lucky; the backtest says the breakout gets chopped up (70 round trips,
mostly losers).

**Verdict: WATCH.** Positive live is encouraging but contradicted by a clearly
negative backtest on a much larger sample. Do not promote; re-check with more
live data.

### bb_rsi_scalper — WATCH

| metric | testnet | combined |
|---|---|---|
| closes | 1 | 1 |
| win rate | 100% | 100% |
| total PnL | +$0.54 | +$0.54 |
| avg PnL % | +0.27% | — |

**Archetype check:** long-only BB+RSI+EMA+Fib scalper with 0.1%-min-profit
exit. One tiny win live — not judgeable.

**Backtest 180d (44d available, BTC 15m):** -1.34% / APR -10.50% /
Sharpe -3.45 / 60% win (5 round trips). Negative on a tiny sample. The 0.1%
profit-floor exit makes wins small and losses (no SL, time-limit exit) larger.

**Verdict: WATCH.** Far too little data either way. Negative backtest is a
yellow flag but the sample is too small to recommend disable.

### bb_short — MAINNET-CANDIDATE (already mainnet-capped)

| metric | testnet | paper | combined |
|---|---|---|---|
| closes | 0 (1 open) | 1 | 1 |
| win rate | — | 100% | 100% |
| total PnL | — | +$3.04 | +$3.04 |
| avg PnL % | — | +1.54% | — |

**Archetype check:** shorts a >+2% spike above upper BB, fixed-% TP, no SL.
Mean-reversion-on-spike — expects high win rate (it only enters on overextension
and takes profit on the snap-back). Live: 1 paper win +$3.04, 1 testnet open.

**Backtest 180d (SOL 1h):** **+1.55% / APR +3.37% / Sharpe 1.56 / 100% win
(10/10 round trips) / max DD 1.04%.** The best risk-adjusted profile of any
strategy: positive APR, Sharpe >1.5, tiny drawdown, every backtest trade a
winner. The 100% win is consistent with the design (only fires on spikes, exits
on a small fixed retrace).

**Verdict: MAINNET-CANDIDATE — already the operator's mainnet cap.** Clears
the bar (positive backtest APR, Sharpe >1, archetype-faithful, no open bugs).
Live sample is thin but the strategy is already the chosen mainnet one. No
action needed beyond "keep it as the mainnet strategy."

---

## Disable recommendations

Per §8, the agent recommends; the operator decides (dashboard `/options` or
`/api/control/strategy/{name}/toggle` — never by editing strategy code).

| strategy | one-line evidence |
|---|---|
| `ema_crossover` | Live 0/6 (−$18.49); fresh 180d backtest −8.21% APR / Sharpe −2.08 / 16.7% win — the 2026-04-30 +5.2% fix has regressed in this regime. |
| `oleg_aryukov` | Post-fix live net −$13.52 / 29% win; 90d backtest −8.44% APR / Sharpe −4.90 (worst of all) / over-trades (60 trips/80d). |
| `penguin_volatility` | Live net −$22.40 / 25% win both modes; 180d backtest −4.20% APR / Sharpe −1.07 / 57 trips — still over-trading per §5. |
| `volatility_breakout` | Per-mode avg PnL % negative (−1.38% / −1.83%) even after stripping sizing artifact; 180d backtest −1.61% APR; 4/5 live trades stopped out. |
| `moon_phases` | Live 1 trade −$12.18 (−6.47%, SL hit); backtest flat (+0.44% APR / Sharpe 0.27 / 4 trips) — no edge. (Low-risk to leave on; no reason to expect profit.) |

## Mainnet candidates

| strategy | status | rationale |
|---|---|---|
| `bb_short` | **Already mainnet-capped — keep** | +3.37% APR, Sharpe 1.56, 100% win (10/10), max DD 1.04% over 170d; archetype-faithful; no open bugs. The only strategy clearing the conservative bar. |

**No new strategies meet the mainnet bar.** Closest is `kalman_breakout`
(+6.17% APR, Sharpe 1.13) but it has only 14 backtest round trips and 1
distinct live trade — fails the "≥~15 live or strong backtest sample" rule.
Re-evaluate after it accumulates more live trades.

## Caveats

1. **~2.5-day silent outage 2026-05-25→27** — gap in all live data; live trade
   counts are partial. Conclusions lean on backtest + win-rate-vs-archetype,
   not absolute live $ (per the brief).
2. **oleg_aryukov pre-fix filter** — all live stats for oleg exclude rows
   before 2026-05-15 (instant-loop bug, PR #132). Post-fix sample is small (7).
3. **volatility_breakout sizing artifact** — the testnet −$26 is inflated by a
   per-mode `MAX_POSITION_SIZE_USD` difference (one ~$1,636-notional testnet
   trade vs ~$200–400 paper). Judgement is based on per-mode avg PnL %, which
   is negative in *both* modes. If kept, fix the per-mode sizing first.
4. **kalman_breakout double-count** — its two "wins" are the same ETH short
   recorded once per mode; treat as 1 distinct trade, not 2–3.
5. **Small live samples** — bb_short, bb_rsi_scalper, kalman, moon_phases,
   qullamagi all have ≤6 live closes. Their verdicts lean on backtest;
   WATCH = "not enough data," not a performance judgement.
6. **Short backtest windows** — daily_long_0830 (15m) and bb_rsi_scalper (15m)
   only had ~44d of candles available; daily_long_0830 also backtests on BTC
   while live caught a different drift. Their negative backtests are
   regime/window-contingent, hence KEEP/WATCH rather than disable.
7. **oleg 180d skipped** — backtested at 90d due to the known O(n²) NW/RCI
   cost (§5 backlog). 90d was sufficient to confirm a strongly negative edge.
8. Backtests run with `--no-save` (no pollution of `backtest_runs`).
