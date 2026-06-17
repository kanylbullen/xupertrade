# Strategy Evaluation — 2026-05-29

Deep per-strategy, per-mode review of all 13 non-`reconciled` strategies.
Data pulled fresh from prod Postgres (all modes) + 180d backtest cross-check
per strategy. **This document RECOMMENDS; the operator DECIDES** (CLAUDE.md §8).
Nothing was disabled, enabled, or otherwise mutated — read-only.

**Today:** 2026-05-29. Live data extends to 2026-06-17 (most recent trade).

---

## TL;DR — one-line verdict per strategy

| Strategy | Verdict | Net PnL (all modes) | One reason |
|---|---|---|---|
| **kalman_breakout** | **KEEP / WATCH-for-mainnet** | **+$68.03** | Best live performer; +6.23% APR / Sharpe 1.14 backtest. Only 2 live trades — sample too thin to promote yet. |
| **bb_short** | **WATCH (mainnet candidate, conservative)** | **+$2.86** | Only gross-positive backtest with clean stats (Sharpe 1.56, 100% win, 1% DD) AND already the allowlisted mainnet strategy. Live sample tiny (1 paper close). |
| **hash_supertrend** | **WATCH** | **+$4.65** | Paper +$8.24 but testnet -$3.59; backtest flat (-0.36% APR). Mode-divergent, undecided. |
| **daily_long_0830** | **WATCH** | **+$7.18** | Live net-positive (+$7.18), but 180d backtest is -20% APR / Sharpe -5.3. Live luck vs negative expectancy — needs more bars. |
| **hash_momentum** | **WATCH** (lean DISABLE) | **+$16.55** | Live paper +$12.71 saves it, but 54+ testnet/paper trades at 24-33% win and backtest -3.2% APR / Sharpe -0.89. On the over-trading watchlist. |
| **qullamagi_breakout** | **WATCH** | **+$6.93** | Live +$6.93 on 6 trades, but backtest brutal: 70 round trips, 21% win, -5.3% APR. Live sample tiny; backtest says marginal. |
| **bb_rsi_scalper** | **WATCH** | **+$0.36** | 1 trade. No signal. Keep collecting. |
| **moon_phases** | **WATCH** | **-$12.35** | Single -$12.18 live loss, but backtest is +0.44% / breakeven over 130d. One unlucky trade ≠ broken. |
| **oleg_aryukov** | **RECOMMEND DISABLE** | **-$15.94** | Net-negative in BOTH modes POST-fix (the fix did NOT rescue it). 25-38% win on a mean-reversion-ish ensemble. |
| **ema_crossover** | **RECOMMEND DISABLE** | **-$19.84** | 0% win across 6 live trades, -8.2% APR / 16.7% win backtest. Trend-follower that never catches a trend in this regime. |
| **penguin_volatility** | **RECOMMEND DISABLE** | **-$24.28** | Net-negative both modes, 25% win live, -4.2% APR backtest. §5 over-trading flag confirmed. |
| **volatility_breakout** | **RECOMMEND DISABLE** | **-$42.18** | Worst net loss. Heavy fee-bleed ($104 fees on 116 backtest trades) eats a 58% win rate into a net loss. |
| **rsi_momentum** | **WATCH** (effectively dormant) | **+$1.15** | 1 trade back in April; no recent activity. Not trading — harmless, watch. |

**Aggregate net across all strategies/modes: +$12.25.** The book is barely
positive, carried almost entirely by `kalman_breakout` (+$68). The four
DISABLE candidates together account for **-$102** of drag.

---

## ALARMING? No active large bleed.

- **No strategy is bleeding large money right now.** The most recent sizable
  losses were `volatility_breakout` testnet (-$19.82 since 05-20, last trade
  06-14) and `moon_phases` (-$12.18, last 06-02) — both modest and stale.
- **oleg loop did NOT recur.** Post-fix oleg has only 4 testnet + 3 paper
  closes — normal cadence, not the instant-stop-out hundreds-of-trades loop.
- **No strategy is trading when it shouldn't.** mainnet shows 0 strategy
  closes (only two $0.01-fee `hash_momentum`/`penguin_volatility` rows from a
  2026-05-10 probe; no PnL). The mainnet idle-by-design cap is holding.

---

## Disable recommendations

Disable command template (key suffix is **`disabled`**, NOT
`disabled_strategies`). **DO NOT RUN — operator decides.** Disable per mode
that's running it (paper + testnet):

```bash
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:testnet:control:disabled <name>
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:paper:control:disabled   <name>
```

### 1. `volatility_breakout` — net **-$42.18** (worst)
- Live: paper -$12.81 net (0% win, 2 trades), testnet **-$29.37 net** (33% win, 3 trades, worst single -$20.03).
- Backtest 170d: -1.61% APR, Sharpe -0.23, **58.6% win but $104.47 fees** on 116 trades — a textbook fee-bleed: gross-ish-flat strategy whose edge is entirely consumed by trading costs (CLAUDE.md §8 criterion).
- Archetype = Keltner breakout w/ trailing stop; should have <40% win + big wins. It has the opposite (high win, small wins, fat tail losses) — the trailing stop is shaving winners while letting the -$20 loss through.
```bash
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:testnet:control:disabled volatility_breakout
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:paper:control:disabled   volatility_breakout
```

### 2. `penguin_volatility` — net **-$24.28**
- Live: paper -$7.93 net, testnet -$16.35 net. **25% win in BOTH modes.** Worst single -$16.17.
- Backtest 170d: -4.19% APR, Sharpe -1.07, **35% win on 115 trades, $51.67 fees** — the over-trading signature flagged historically in §5 (-10.3% in the original 180d run; still negative after the `use_timing_filter=False` Pine-default fix).
- Archetype = BB/KC volatility-state long-only; should be selective. 57 round trips in 170d backtest = not selective. Diverges hard.
```bash
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:testnet:control:disabled penguin_volatility
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:paper:control:disabled   penguin_volatility
```

### 3. `ema_crossover` — net **-$19.84**
- Live: **0% win across all 6 trades** (paper -$9.92 net, testnet -$9.92 net). Avg -$3/trade.
- Backtest 170d: -8.20% APR, Sharpe -2.08, **16.7% win** (4W/20L).
- Archetype = 7/19 EMA cross trend-follow; expects low win but big trend wins. The phantom-reversal bug was already removed (§5), so this is the *clean* port still losing — the strategy simply has no edge in the current chop. 6 consecutive live losses with zero wins is unambiguous.
```bash
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:testnet:control:disabled ema_crossover
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:paper:control:disabled   ema_crossover
```

### 4. `oleg_aryukov` — net **-$15.94** (post-fix)
- **Pre/post-fix split (cutoff 2026-05-15):** pre-fix loss was negligible (paper -$0.16 gross). The losses are **all POST-fix**: paper post-fix -$5.91 net (3 trades), testnet post-fix -$8.88 net (4 trades). The `_entry_bar_ts` fix (PR #132/#130) stopped the loop but did **not** make the strategy profitable.
- 25% (testnet) / 38% (paper) win on a 6-indicator confirmation ensemble that should be *high*-win by design. Backtest is O(n²) and times out (§5 known) — but live post-fix evidence alone is sufficient.
```bash
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:testnet:control:disabled oleg_aryukov
docker exec hypertrade-redis-1 redis-cli SADD hypertrade:paper:control:disabled   oleg_aryukov
```

**DISABLE shortlist total drag: -$102.24 net.**

`hash_momentum` is a borderline 5th candidate (lean-disable): 54+ live trades
at 24-33% win, backtest -3.2% APR / Sharpe -0.89, on the §5 over-trading
watchlist. It is held at WATCH only because live paper is +$12.71 — a thin
reprieve. If the operator wants a cleaner book, disabling it is defensible;
recommend one more week of testnet data first to confirm the paper gain isn't
mode-size luck.

---

## Mainnet-promotion recommendation: **none yet (conservative)**

Mainnet is real money. The bar is: gross-positive AND low-DD across BOTH live
and backtest, with enough sample. **No strategy clears it cleanly.**

- **`kalman_breakout`** is the strongest performer (+$68 live, +6.23% APR /
  Sharpe 1.14 / 5.3% DD backtest) — but only **2 live closes total** (1 paper,
  1 testnet, both the same 05-07→06-07 trade). A single +$34 trade is not a
  track record. **Hold for sample.** This is the one to promote *if* it logs
  ~8-10 more profitable round trips over the next few weeks.
- **`bb_short`** is the cleanest *backtest* (+3.37% APR, Sharpe 1.56, 100% win,
  1.04% DD) AND is already the lone strategy on the
  `HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES=bb_short` cap — i.e. it is already
  the chosen mainnet candidate, gated behind per-tenant UI allowlist. Live
  sample is 1 paper close (+$2.86). **Recommendation: leave the cap as-is;**
  the existing `bb_short` allowlist entry already encodes the conservative
  bet. Do NOT add a second strategy this cycle.

**If/when promoting (steps, for reference — do not run now):**
1. Add the name to the `HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES` Phase value
   (app `xupertrade`, env `Development`), comma-separated.
2. Redeploy so the mainnet bot picks up the new cap (`phase run -- docker compose ... --force-recreate`).
3. Enable per-tenant via the dashboard UI (Settings → mainnet strategy toggle,
   PR #133 per-tenant allowlist). Both layers must agree before a mainnet trade fires.

---

## Per-strategy detail

PnL is **net** (gross − fees) unless noted. `closes` = close-side rows only.

### High-trade-count (most signal)

**hash_momentum** — *momentum, SOL 4h*
| mode | closes | win% | gross | fees | net | avg |
|---|---|---|---|---|---|---|
| paper | 39 | 33% | 19.91 | 7.21 | **+12.71** | 0.51 |
| testnet | 25 | 24% | 7.98 | 4.14 | **+3.84** | 0.32 |
| mainnet | 0 (probe) | — | — | 0.01 | — | — |
- Backtest 144d: -3.20% APR, Sharpe -0.89, 32.6% win, $41.23 fees on 46 round trips.
- Archetype = momentum (<40% win OK if wins are big). Wins ARE big (best +$13.91) but frequency of small losses (26 paper / 19 testnet) + fees erode it. Backtest negative. **WATCH, lean-disable** — paper green is the only thing keeping it.

**daily_long_0830** — *time-of-day long, BTC 15m*
| mode | closes | win% | gross | fees | net |
|---|---|---|---|---|---|
| paper | 9 | 56% | 4.69 | 1.80 | +2.89 |
| testnet | 20 | 60% | 8.27 | 3.98 | +4.29 |
- Backtest 44d: **-20.37% APR, Sharpe -5.32**, 44% win, $39 fees on 43 round trips.
- Live is +$7.18 net and 56-60% win — but the backtest is one of the worst in the book. The strategy buys 08:30 / sells 08:00 daily; live luck in this window vs. structurally negative expectancy. **WATCH** — if live win rate reverts toward the 44% backtest, disable.

### Cross-check / smaller-sample

**kalman_breakout** — *Kalman bands, ETH 1h.* Live +$68.03 (2 trades, both the same +$34 round trip mirrored across paper/testnet). Backtest +6.23% APR / Sharpe 1.14 / 35.7% win / 5.3% DD. Best risk-adjusted profile in the book. **KEEP; future mainnet candidate once sample grows.**

**bb_short** — *BB upper-breakout short, SOL 1h.* Live +$2.86 (1 paper close). Backtest +3.37% APR / Sharpe 1.56 / **100% win / 1.04% DD** / only $8.93 fees on 10 round trips. Cleanest backtest. Already the allowlisted mainnet strategy. **WATCH / hold mainnet cap.**

**hash_supertrend** — *SuperTrend flip, BTC 1h.* Paper +$8.24 (50% win, best +$28.55) vs testnet -$3.59. Backtest flat (-0.36% APR, Sharpe -0.06, 42% win, $75 fees on 83 round trips). Mode-divergent + backtest-neutral. **WATCH.**

**qullamagi_breakout** — *multi-MA breakout, ETH 1h.* Live +$6.93 (6 trades, 67% win). Backtest ugly: **21.4% win, 70 round trips, -5.3% APR, $62.90 fees.** Live sample far too small to trust over the backtest. **WATCH** — the backtest says this over-trades like penguin/volatility_breakout; watch for the same fee-bleed live.

**moon_phases** — *lunar calendar long, BTC 1d.* Live -$12.35 (1 trade, the one full-moon entry that stopped out). Backtest +0.44% / 50% win / breakeven over 130d / 0.97% DD. One unlucky trade, not a broken strategy. Trades ~monthly so sample accrues slowly. **WATCH.**

**bb_rsi_scalper** — *BB+RSI+EMA+Fib scalper, BTC 15m.* 1 testnet close, +$0.36. No signal yet. **WATCH.**

**rsi_momentum** — 1 paper close back on 2026-04-17 (+$1.15), nothing since. Effectively dormant / not emitting signals. Harmless. **WATCH** (verify the feed/signal path is alive if it stays silent another 2 weeks).

### Disable candidates — see "Disable recommendations" above
`oleg_aryukov` (-$15.94 post-fix), `ema_crossover` (-$19.84), `penguin_volatility` (-$24.28), `volatility_breakout` (-$42.18).

---

## Caveats

1. **~2.5-day silent outage 2026-05-25→27** — no trades any mode in that
   window. Live samples are smaller than the calendar implies; per-strategy
   live trade counts here are single digits to low-double-digits — treat all
   live win-rates as directional, not statistically settled.
2. **Mode size differs.** Testnet position size was ~4× paper in the last
   weekly eval, so identical logic shows a bigger testnet $ swing for the same
   signal. Compare win-rate/direction across modes, not raw $. (E.g.
   penguin's -$16 testnet vs -$8 paper is the same 25%-win strategy at
   different size.)
3. **Fees materially decide several verdicts.** volatility_breakout ($104
   backtest fees), hash_momentum ($41), qullamagi ($63), penguin ($52) all
   bleed to fees. A gross-flat strategy here is a net loser.
4. **mainnet idle by design** — operator cap + per-tenant allowlist. The only
   mainnet rows are $0.01-fee probes from 2026-05-10; no mainnet PnL exists to
   evaluate.
5. **oleg pre-fix separation** — pre-2026-05-15 oleg trades are negligible
   (-$0.16 gross paper, 0 testnet closes), so the post-fix loss is NOT
   contaminated by the old loop bug. The post-fix loss is real.
6. **oleg backtest unavailable** — O(n²) Nadaraya-Watson/RCI loops time out on
   a 180d window (known §5 backlog item). Disable rec rests on live post-fix
   evidence alone, which is sufficient (net-negative both modes).
7. **kalman/bb_short "two trades" are one trade mirrored** across paper+testnet
   — do not read the +$68 kalman headline as two independent confirmations.
