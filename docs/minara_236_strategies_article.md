# We found 21 money-printers after backtesting 236 TradingView strategies

**Author:** Minara AI (@minara)
**Published:** 2026-04-15
**Source:** https://x.com/minara/article/2044432012002635843

---

## TL;DR

- We tested 236 public TradingView strategies trade-by-trade under HyperLiquid's real fees - 63 passed strict replication. 36 were profitable. 21 cleared 10% annualized return
- Trade frequency is the main fee killer: every strategy that flipped from profitable to losing traded 200+ times per year
- Low-frequency strategies kept almost all of their edge. The best: 16 trades in 90 days, +204% annualized
- The industry-standard ±15% PnL bar would have passed ~103 strategies. Trade-by-trade matching passed 63. The gap is strategies that look replicated but aren't
- We also tried to fix 4 broken strategies. In-sample: all improved. Out-of-sample: one test showed the gains didn't hold

---

Over the past three months, our team built a pipeline that collects public PineScript strategies, rebuilds each one in our internal-testing [Strategy Studio](https://minara.ai/app/trade/strategy-studio), replicates every trade against TradingView's own backtest engine, and re-runs everything under HyperLiquid's real fee structure (0.015% maker, 0.045% taker). We ran 236 strategies through this pipeline on major pairs (BTC, ETH, SOL, XRP, BNB, DOGE) and supported timeframes, only 21 cleared 10% APR.

This article covers the pipeline, the aggregate results, and four specific strategies that tell different stories about what happens between a TradingView screenshot and a live order book.

## The pipeline

The full process runs in four stages:

1. **Collect and select.** We crawled publicly available PineScript v5 strategies from TradingView's community library, filtered for strategies trading major pairs (BTC, ETH, SOL, XRP, BNB, DOGE) on supported timeframes, and rebuilt 236 of them in Strategy Studio.

2. **Align.** Each strategy ran on Strategy Studio under identical conditions: same capital, commission, position sizing, and date range as the original TradingView backtest. We matched every trade on three conditions: same direction, entry price within 1%, and exit price within 1%. A strategy passed alignment if at least 70% of its TradingView trades had a matching Strategy Studio trade, and the total trade count differed by no more than 10% (or 3 trades). 63 of 236 passed: 21 at the stricter "A" bar (≥90% match, ≤10% PnL divergence), 42 at "B". The remaining 173 showed trade-level discrepancies that no sensible parameter tuning would close.

3. **Re-backtest at zero fees.** All 63 aligned strategies ran again on standardized windows (14 days for 1m, 60 days for 5m, 90 days for 15m, 365 days for 30m, 730 days for 1h, 1,460 days for 4h and 1d) with $10,000 starting capital and zero fees. This isolates the strategy logic from execution costs.

4. **Re-backtest at HyperLiquid fees.** Same windows, same capital, but with maker 0.015% and taker 0.045%. The backtest engine simulates fees only; a live HyperLiquid market order reserves additional slippage tolerance, but we do not model that here because it would reduce comparability with TradingView's own report. This is the number that matters if you plan to trade it.

The alignment step alone took weeks of debugging. Differences in how TradingView and Strategy Studio handle bar magnets, DST transitions, and contract-based position sizing required manual corrections for several strategies. The total engineering effort exceeded 300 hours.

## Numbers

Of the 63 aligned strategies with recent dual-fee backtest data:

- 21 produced annualized returns above 10% (Tier 1)
- 15 were profitable but below 10% APR (Tier 2)
- 27 lost money (Tier 3)
- 57% of strategies remained profitable after real fees
- Median APR after fees: +2.4%
- Median Sharpe after fees: +0.41

The 21 money-maker strategies are:

1. [Optimized BTC Mean Reversion (RSI 20/65)](https://www.tradingview.com/script/pIrgsDpT/) — BTCUSDT 15m, +204.6% APR
2. [Volatility Breakout System [Fixed Risk]](https://www.tradingview.com/script/36zwwSMa/) — ETHUSDT 1h, +124.6% APR
3. [SuperTrend AI Adaptive - Strategy [BTC]](https://www.tradingview.com/script/kZVrTReu/) — BTCUSDT 4h, +60.2% APR
4. [BB Upper breakout Short +2% (dr Ziuber)](https://www.tradingview.com/script/UBGvlIlq/) — SOLUSDT 1h, +48.1% APR
5. [SuperTrend STRATEGY](https://www.tradingview.com/script/VLRj2sG9/) — BTCUSDT 1d, +35.6% APR
6. [Penguin Volatility State Strategy](https://www.tradingview.com/script/skzo4i9e/) — BTCUSDT 1d, +34.5% APR
7. [MACD Zero-Line Strategy (Long Only)](https://www.tradingview.com/script/llTXO45e/) — BTCUSDT 1d, +34.5% APR
8. [CDC BACKTEST (MACD) FIX AMOUNT $200k per trade](https://www.tradingview.com/script/7nv3hTpO/) — BTCUSDT 1d, +34.5% APR
9. [Hash Momentum Strategy](https://www.tradingview.com/script/L6VNlhiV/) — BTCUSDT 4h, +32.8% APR
10. [Moon Phases Long/Short Strategy](https://www.tradingview.com/script/sl42otOB/) — BTCUSDT 1h, +29.9% APR
11. [7/19 EMA Crypto strategy](https://www.tradingview.com/script/c0dAzn2Q/) — ETHUSDT 30m, +28.4% APR
12. [RSI > 70 Buy / Exit on Cross Below 70](https://www.tradingview.com/script/wZIdSrBG/) — BTCUSDT 4h, +24.3% APR
13. [50 & 200 SMA + RSI Average Strategy (Long Only, Single Trade)](https://www.tradingview.com/script/1x2AawHf/) — ETHUSDT 1d, +23.5% APR
14. [Kadunagra-Pivot Point SuperTrend-trades analysis](https://www.tradingview.com/script/b74KzneI/) — BTCUSDT 4h, +23.2% APR
15. [ETHUSDT 4H - Keltner Breakout](https://www.tradingview.com/script/LmNV3ZLN/) — ETHUSDT 4h, +21.0% APR
16. [Hash Supertrend [Hash Capital Research]](https://www.tradingview.com/script/6zYF9Xts/) — SOLUSDT 4h, +15.2% APR
17. [Crypto LONG PY](https://www.tradingview.com/script/3Uel153a/) — SOLUSDT 5m, +12.2% APR
18. [Oleg_Aryukov_Strategy](https://www.tradingview.com/script/R4mgYcZ5/) — BTCUSDT 15m, +10.9% APR
19. [Options test Daily Long 08:30 Exit next day 08:00 UTC](https://www.tradingview.com/script/DJT1l5tH/) — ETHUSDT 5m, +10.8% APR
20. [Qullamagi EMA Breakout Autotrade (Crypto Futures L+S)](https://www.tradingview.com/script/0rVYn2c4/) — ETHUSDT 1h, +10.5% APR
21. [Kinetic Kalman Breakout](https://www.tradingview.com/script/nd8EpyQ5/) — ETHUSDT 15m, +10.1% APR

The top of the Tier 1 table under HyperLiquid fees is not what a trend follower would expect. The best performer is an optimized BTC mean-reversion strategy on 15m that placed 16 trades in 90 days and returned +204% annualized, with a Sharpe ratio above 4. Second is an ETH 1h momentum strategy at +124% APR. Third is an adaptive SuperTrend on BTC 4h at +60% APR. The top of the leaderboard mixes mean reversion, momentum, and trend following. No single approach dominated.

The middle of the table is where fee sensitivity starts to bite. The 15 Tier 2 strategies all made money, but barely. Many of them would have ranked Tier 1 at zero fees and dropped below the 10% threshold only when fees were applied. Their logic works; their trade frequency erodes the edge.

At the bottom of the distribution, losses accelerate. The pattern is the same each time: high trade count, low win rate, thin per-trade margin. Buy Sell Signal on BTC 1m is the canonical example we have seen across iterations of this pipeline: 2,655 trades in 14 days, a -64.7% loss on capital, which extrapolates to -1,687.6% APR. It paid more in fees than it could have possibly earned.

The strategies with very high win rates in this dataset are all built the same way: take a small fixed profit, exit quickly, re-enter. BB Upper Breakout Short +2% on SOL 1h hit 100% over 49 trades. Crypto LONG PY on SOL 5m hit 100% over 39 trades. OrangePulse v3.0 Lite on BTC 1h hit 94.9% over 118 trades. These are mean-reversion strategies with small, fixed profit targets, and their structure is also their weakness. Each winner is small, and the round-trip fee eats a constant 0.06%. When the average winner makes 1.5%, fees consume roughly 4% of the edge before anything else. OrangePulse survived this with a +0.1% APR. The next strategy down the list did not.

The inverse profile looks worse on paper and performs better in practice. RSI > 70 Buy on BTC 4h has a win rate around 35%. It loses nearly two trades out of three. SuperTrend AI Adaptive sits near 48%. Keltner Breakout near 34%. These are trend-following strategies where most trades lose a little and a few trades win a lot. The occasional large winners absorb fee costs without effort. Most of the Tier 1 strategies by annualized return after fees have win rates between 35% and 50%. A low win rate paired with a high profit factor is the profile that survived.

SuperTrend STRATEGY, a Tier 1 performer on BTC 1d, carried a 46.1% max drawdown across its 4-trade history. RSI > 70 Buy also cleared Tier 1 with a 14.8% max drawdown and a Sharpe ratio above 2. Holding through a 46% drawdown is a different experience than holding through a 15% one, even if the final number at the end of the period is larger.

Sharpe ratios sort the dataset differently than raw APR. The highest Sharpe in Tier 1 belongs to a 15-minute mean-reversion strategy that took very few trades over a short window. RSI > 70 Buy and SuperTrend AI Adaptive are also near the top on risk-adjusted measures. A low APR with a contained drawdown and a high Sharpe can represent a more tradeable position than a high APR that requires surviving near-ruin first.

Profit factors tell a related story. SuperTrend's 8.98 means its total profits were nearly nine times its total losses. That ratio explains why four trades were enough to rank in Tier 1. Other Tier 1 strategies get their edge from many smaller wins, per-trade margins distributed across dozens or hundreds of positions. Both work. They work differently.

The top of the Tier 1 list mixes mean reversion, momentum, and trend following. Complex multi-factor systems and simple single-parameter strategies both appear. Neither complexity nor simplicity predicted returns consistently across the 63 strategies. What predicted returns was the match between the strategy's per-trade profile and the fee environment it would trade in. No indicator or filter combination dominated Tier 1: trend following appeared most often, but Bollinger Bands, Keltner channels, EMA crossovers, MACD, and calendar-based entries all produced Tier 1 members.

The primary driver of fee impact was not strategy type, asset, or timeframe. It was trade count. Of the 63 strategies in the analysis pool, 14 were profitable at zero fees and lost money once HyperLiquid fees were applied. Every one of those 14 traded above 200 times per year. Strategies that traded fewer than 25 times per year retained almost all of their zero-fee return.

The strategies skewed toward trend following, with the most common timeframes being 4h and daily. BTC was the most-traded asset, but the pool also includes ETH, SOL, XRP, BNB, and DOGE strategies.

The aggregates above flatten 63 strategies into averages and tiers. The four cases below break the averages apart: a mean-reversion short with a 100% win rate, a trend follower that fires four times in four years, a momentum play that buys overbought conditions, and a classic moving-average strategy that beat a losing market. Each one looks wrong on paper. Each one worked.

(All cases below were run in the internal test build of Strategy Studio. To apply for early access, see the link at the end of this article.)

## Case 1: The 100% win rate short, BB Upper Breakout Short +2% by [@DrZiuber](https://x.com/drziuber), SOLUSDT, 1h

This strategy shorts SOL when the price breaks 2% above the upper Bollinger Band (20-period, 2 standard deviations) and exits at 2% profit. It is a textbook mean reversion play: when an asset spikes too far above its recent range, sell it and wait for the pullback.

Over 730 days, it placed 49 trades. Every single one was profitable. Win rate: 100%.

Under HyperLiquid fees, it returned +96.3% total, or +48.1% annualized. The fee drag was 3%, almost nothing, because the strategy trades infrequently and takes a meaningful profit on each position.

The alignment check was clean: Strategy Studio matched TradingView's results within 2%, with identical trade counts.

There are obvious caveats. Forty-nine trades over two years is a small sample. The strategy only works on SOL, which has had a specific volatility profile during this period. A 100% win rate over 49 trades does not imply a 100% win rate over the next 49. And the max drawdown was 36.7%, meaning at one point the strategy was sitting on a significant unrealized loss before the reversion completed. The caveats are real. So is the edge.

## Case 2: Four trades in four years, SuperTrend STRATEGY by [holdon_to_profits](https://www.tradingview.com/script/VLRj2sG9-SuperTrend-STRATEGY/), BTCUSDT, 1d

Most strategies try to catch as many moves as possible. This one does the opposite. It runs a classic SuperTrend indicator (ATR period 10, multiplier 8.5) on BTC daily candles and goes long when the trend flips bullish. The high multiplier means it ignores almost everything. Only major trend reversals trigger a signal.

Over four years (1,460 days), it placed four trades. Three were winners, one was a loser. Total return under HyperLiquid fees: +292.4%, or +73.1% annualized.

The fee drag was 1%. Four trades in four years means the strategy paid fees eight times total (entry and exit). Under HyperLiquid's fee structure, that cost was functionally irrelevant.

The Sharpe ratio was 1.24. The profit factor was 8.98. The max drawdown was 46.1%, which is large but expected. When you hold BTC through a trend reversal with no stop loss, you ride the drawdown until the SuperTrend flips.

Four trades is a small sample even when they cover four years of daily candles. The profit factor of 8.98 is accurate for the period tested; the confidence interval around that number is wide. The rank is real. It is also partly an artifact of a narrow trade count on a favorable window.

The alignment with TradingView was tight: 7% PnL divergence, identical trade count (7 trades during the TV backtest window), identical win rate.

The SuperTrend with a high multiplier is barely a strategy at all. It is closer to "buy BTC when it starts a bull market, sell when it ends." Of the 37 strategies we tested, 34 used more signals, more filters, and more logic. This one used ATR and a multiplier. It ranked second.

## Case 3: Buying overbought, RSI > 70 Buy / Exit on Cross Below 70 by [Boubizee](https://www.tradingview.com/u/Boubizee/), BTCUSDT, 4h

Every beginner learns the same rule: RSI above 70 means overbought, time to sell. This strategy does the opposite. It buys BTC when RSI(14) crosses above 70 and exits when RSI drops back below 70. It is a momentum continuation play: the assumption is that a strong RSI reading signals strength, not exhaustion.

Over 1,460 days, it placed 142 trades. Win rate: 35.2%. Under HyperLiquid fees, it returned +99.7% total, or +24.9% annualized.

The Sharpe ratio was 1.85. The max drawdown was 14.8%. +24.9% annually with a 14.8% max drawdown is a better risk-adjusted profile than most strategies in our dataset that returned higher nominal APR.

Fee drag was 21%, meaningful but not disqualifying. At 142 trades over four years, the round-trip cost on each trade averaged roughly 0.06%. The per-trade edge was large enough to survive it.

Alignment was clean: 2% PnL divergence against TradingView, with matching trade counts.

Indicator thresholds are heuristics, not laws. On BTC at 4h, over four years and 142 trades, RSI above 70 more often meant momentum than exhaustion.

## Case 4: Beating a losing market, 50 & 200 SMA + RSI Average Strategy by [muratkbesiroglu](https://www.tradingview.com/script/1x2AawHf/), ETHUSDT, 1d

This strategy uses one of the oldest ideas in trend following: go long when price sits above both the 50-day and 200-day SMA, and when a smoothed RSI (9-period average of RSI-21) is above 57. Exit when price drops below the 50-day SMA and the RSI average falls back under 57. Long-only, one position at a time, no leverage.

Over the test window, it placed 19 trades. Under HyperLiquid fees, it returned +95.1% while ETH buy-and-hold returned -22.1%. The strategy beat the market by +117 percentage points in a period when simply holding ETH lost money.

The Sharpe ratio was 1.16. The strategy stayed out of the market during most sideways and down periods, which is why it outperformed buy-and-hold by such a wide margin: the 57-threshold RSI filter forced it to sit in cash during low-momentum conditions that dragged long-term holders.

The alignment check was near-perfect: 19 of 19 trades matched TradingView's engine, with PnL within 0.1 percentage points (1,072.0% vs 1,071.9% over the TV test window).

Most strategies in this article beat buy-and-hold during a period when buy-and-hold was doing well. This one beat buy-and-hold during a period when buy-and-hold was losing. That is a different kind of test, and a harder one. The textbook answer still works; the discipline to stay out of the market during weak periods is what makes it work.

## Optimization

The four cases point at the same pattern from different angles. What made them work was not indicator choice, win rate, or complexity. It was the match between per-trade edge and fee cost: each strategy either traded infrequently enough to preserve its edge, or took profits large enough to absorb the round-trip. The SuperTrend on BTC 1d paid fees eight times in four years. The mean-reversion short on SOL traded 49 times across 730 days. The RSI continuation play won only one trade in three, but the winners were large enough that fees barely registered. The SMA strategy's edge was not in what it traded but in what it skipped: the RSI filter kept it in cash during the periods that dragged long-term holders underwater.

Replication is one thing Minara does. The four cases below show another: take a strategy from the losing end of the dataset, identify what is structurally wrong, and fix it. The fixes range from a four-line patch to a complete rewrite, and not all of them held up on out-of-sample data. These are the typical optimization cases we encountered during testing.

## Rescue 1: Buy Sell Signal Strategy → Quant Trend Engine-style logic (complete rewrite)

The first target was the Buy Sell Signal Strategy, a 1-minute BTC scalper that placed 2,655 trades in 14 days and lost 64.7% of its capital under HyperLiquid fees. Minara refused to tune the strategy and rewrote it from scratch, borrowing structural elements from high-ranking templates in our dataset. The result is a 4h multi-factor trend follower, structurally unrelated to the original EMA cross logic.

What Minara changed:

- **Timeframe: 1m → 4h.** The 1m window produced 2,655 trades in 14 days. The 4h window produces about 18 trades per year. The per-trade edge needed to survive fees dropped from an impossible 0.09%+ to a reachable 0.2 to 0.5%.

- **Entry: single EMA cross → 8-factor weighted score.** The rewrite requires a minimum score of 5.0 across EMA stacking, slope, separation, momentum persistence, path efficiency, breakout strength, ATR regime, and pullback reclaim. No single factor can fire an entry alone.

- **Added a Path Efficiency filter.** Kaufman Efficiency Ratio ≥ 0.33. This single filter blocks entries during sideways markets, where the original strategy accumulated most of its losses.

- **Stop loss: 0.5 ATR fixed → 2% hard stop + 2.8 ATR trailing.** Tight ATR stops on 1m get eaten by noise. The trail gives real trends room to run.

- **Direction: long & short → long only.** BTC's long-term beta is positive. Shorting a structurally upward-drifting asset gives up edge before any strategy logic applies.

- **5-bar cooldown after exits.** Prevents re-entry into the same noise pattern that just triggered the previous stop.

Minara did not invent this structure. It selected a skeleton similar to Quant Trend Engine, already the top performer in our dataset, and adapted it for the original strategy's asset. Diagnosing when a strategy is structurally unsalvageable and proposing a known-good replacement is more useful than pretending parameter tuning will work.

## Rescue 2: XRP Non-Stop Strategy by [antishyilma81](https://www.tradingview.com/script/J5akHbOr-XRP-Non-Stop-Strategy-TP-25-SL-15/) → XRP Trailing ATR

The second target was the XRP Non-Stop Strategy, a long-only trend-follower using EMA 20/50 filtering with a fixed 25% take profit and 15% stop loss. Over 730 days it returned +26.2% PnL with a 43.8% win rate. The problem was not the nominal return. The problem was a 75.7% max drawdown and a profit factor of 1.04, meaning the strategy survived only because one eventual win happened to offset accumulated losses. Minara kept the skeleton (EMA filter, long-only, 25% take profit, XRP ticker guard) and added four modules around it.

What Minara changed:

- **Stop loss: fixed 15% → 2.5 ATR initial, 2.0 ATR trailing, ratchet-only.** The fixed 15% was either too tight in high-volatility regimes or too loose in low-volatility ones, and it offered no protection on unrealized gains. The ATR trail locks profit as price advances. This single change accounts for most of the drawdown reduction from 75.7% to 14.6%.

- **RSI entry filter: require RSI < 45.** Counter-intuitively strict. With EMA already confirming the uptrend, entries fire only when RSI dips, buying pullbacks instead of breakouts. Win rate rose from 43.8% to 55.8%.

- **ATR regime filter: skip when ATR/price < 1.5%.** Low-volatility windows produce false EMA crosses and tight stops that get scraped. Blocking those windows raised profit factor from 1.04 to 2.99.

- **3-bar cooldown after exits.** Prevents re-entering the same chop pattern that just triggered the stop.

The strategy is trading more but with higher per-trade quality. Minara also reported four alternatives it tested and rejected: a tighter 1.5 ATR trail got scraped by normal pullbacks, shorter EMAs produced more false crosses, a closer 15% take profit capped winners and collapsed the profit factor, and an 8-bar cooldown missed continuation entries. The rejected branches define the parameter space as much as the accepted changes do.

## Rescue 3: EMA 50/200 Pullback + RSI (BTC/USDT 15m - 2 Bar Logic)

The third target was the EMA 50/200 Pullback strategy on BTC 15m, which used a 2-bar pullback-and-reclaim signal with a fixed 0.49% stop and a 1:5 reward ratio. The skeleton was reasonable. The execution scale was wrong: a 0.49% stop on 15m is sitting inside normal noise, and the 1:5 reward ratio looks attractive on paper but is unreachable in practice when most trades stop out before reaching target. Minara changed the timeframe, replaced the risk model, added a trend filter, added a trailing stop, and tightened the entry conditions.

In-sample results (2022-04 to 2025-01, 2.8 years):

What Minara changed:

- **Timeframe: 15m → 1D.** A 0.49% stop on 15m is roughly 0.16 ATR (inside normal noise). On daily, the same percentage is ~0.5 ATR (a meaningful move). The pullback-and-reclaim signal also shifts from a microstructure event to a structural one across two days.

- **Risk model: fixed 0.49% / 2.45% at 1x → ATR-scaled 1.5 / 4.0 at 20x leverage.** Risk-reward drops from a paper 1:5 to a real 1:2.63. The original ratio was unreachable; the new one is actually achieved by 14 of 20 trades. Leverage was raised because 20 trades over 4 years is a low frequency that needs amplified per-trade impact to produce a meaningful return.

- **Trailing stop added: activate at +2 ATR profit, trail by 1 ATR.** Out of 20 trades, 12 exited via trailing stop with average +6.21% gain. Without the trail, those mid-distance winners would have either reversed back to the initial stop or stalled below the take profit.

- **ADX > 20 trend strength filter added.** EMA 50/200 confirms direction but not whether the trend is actually advancing. ADX requires real directional momentum, blocking the "EMA stack looks bullish but price is chopping" environment that produced most of the original strategy's losses. Minara reported this as a separate v2 → v3 iteration so its marginal contribution could be measured independently.

- **RSI threshold: > 50 → bounded 45 to 75 (long).** The original accepted RSI = 85 as a buy signal. The bounded range filters out late-stage entries near overbought tops while keeping the meaningful momentum range.

## Rescue 4: Momentum Strategy → Momentum ATR Exit

The fourth target was the Momentum Strategy, a classic TradingView template: go long when 12-bar momentum is positive and accelerating, go short when both are negative. The entry logic was clean. The strategy had no exit logic at all: no stop loss, no take profit, no timed close. Positions only flipped when the opposite signal triggered. This turned every winning trend into a round-trip: the strategy rode the move up, then gave it all back waiting for the reversal to confirm. Profit factor was 1.01 over 1,165 trades, which is random. Minara's diagnosis: the entry has edge, the exit doesn't exist. The fix was four lines of code.

What Minara changed:

- **Added ATR-scaled stop loss: 1.5 × ATR(14).** Distance scales with current volatility instead of a fixed percentage.
- **Added ATR-scaled take profit: 3 × ATR(14).** A 2:1 reward-to-risk ratio built into every trade.
- **Entry logic: unchanged.** The mom0 > 0 and mom1 > 0 condition and the stop=high+mintick pending order stayed verbatim.

That was the entire change. Everything else (momentum calculation, direction, position sizing, long/short symmetry) was preserved.

## Caveats

These findings hold under specific conditions. Several of those conditions are worth naming.

**Market regime.** The backtest window (2016 to 2026 for the longest timeframes, shorter for higher-frequency strategies) was net bullish for crypto. Several Tier 1 strategies are long-only trend followers that benefit directly from sustained upward drift. In a multi-year bear market, most would have different risk profiles and some would fall out of Tier 1.

**Asset concentration.** The aligned pool skewed toward BTC. ETH, SOL, XRP, BNB, and DOGE strategies are based on smaller samples and may not generalize. Altcoins with different volatility or liquidity profiles were not tested.

**Out-of-sample validation.** Only one strategy in this article (Rescue 3) was tested on data held out from the tuning process. The remaining results should be read as in-sample: strategy logic was evaluated on the same data used to identify it. The optimization cases in particular carry overfit risk that larger OOS samples would be needed to rule out.

**Selection bias, quantified.** We tested 236 public PineScript strategies that survived our initial screen (supported symbol, supported timeframe, sufficient kline history). Of those, only 27% passed trade-by-trade replication. Of the 63 that replicated, 57% remained profitable under HyperLiquid fees. The compound probability that a random TV strategy in our population both replicates its own backtest and makes money net of realistic fees is roughly 15%, about one in seven.

**Alignment bar choice matters.** Moving from ±15% PnL tolerance to trade-by-trade matching dropped our pass rate from roughly 44% to 27%. Different verification standards produce different conclusions. Anywhere in this article we report "aligned", we mean trade-by-trade. Other teams may report higher alignment rates using looser bars; we view that as noise, not signal.

**Fee specificity.** All real-fee numbers assume HyperLiquid's fee structure (0.015% maker, 0.045% taker). Exchanges with higher taker fees would push more strategies into Tier 3. Zero-fee environments would flip several Tier 3 results back to profitable.

**Backtest vs live.** These are backtests. Live execution adds latency, partial fills, and order book effects that pure OHLC replay does not capture.

## What comes next

The aligned strategy library, dual-fee backtest results, and optimization tools described in this article are all coming to Minara Strategy Studio. We are currently in internal testing. When we launch, access will open in batches starting with users already on the waitlist.

https://minara.ai/app/trade/strategy-studio

(Click the input box to sign up.)

As more community strategies emerge, we are planning a "strategy square", where traders can share, discuss, and even earn from your own strategies. Stay tuned.
