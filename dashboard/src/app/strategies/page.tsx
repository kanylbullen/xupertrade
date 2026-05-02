export const dynamic = "force-dynamic";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { TradingViewChart } from "@/components/tv-chart";

const strategies = [
  {
    id: 1,
    name: "supertrend",
    symbol: "BTC",
    timeframe: "1d",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/kZVrTReu/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1d adaptive ST flips, long & short)",
    summary: "Direct port of TradingView 'SuperTrend AI Strategy [Adaptive]' by © DefinedEdge — regime-aware SuperTrend with AI signal scoring and full SL/TP risk management.",
    logic: [
      "SuperTrend on hl2 with ATR(10) and base multiplier 3.0. Multiplier adapts to regime.",
      "Regime detection: atrRatio = ATR / SMA(ATR, 40), combined with ADX(14). Volatile (atrRatio>1.4), Ranging (ADX<20 AND atrRatio<0.9), or Trending (default).",
      "Adaptive multiplier: volatile → base × (1 + (atrRatio-1) × 0.4); ranging → base × 0.85; trending → base. Clamped to [base × 0.5, base × 2.0].",
      "On a trend flip, score the signal 0-100 across 5 factors: volume surge, displacement beyond band, EMA(50) trend alignment, regime quality, and prior band distance.",
      "Entry only if score ≥ 65 AND EMA trend aligns AND regime is not ranging AND volume > SMA(volume, 20). 5-bar cooldown between entries.",
      "Exit: SL = entry ± ATR × 6. TP = entry ± SL_distance × 2.5 (1:2.5 risk/reward). Optional trailing stop disabled by default.",
    ],
    strengths: [
      "Adaptive multiplier widens bands in volatile regimes (fewer whipsaws) and tightens in ranging regimes (rare entries).",
      "5-factor AI score gates every entry — rejects flips that look mechanically valid but lack quality signals.",
      "Long & short with structured SL/TP gives bounded per-trade risk and a known reward profile.",
      "Skipping ranging regime entirely avoids the worst drawdowns of mechanical trend-followers.",
    ],
    weaknesses: [
      "Stack of filters (score ≥ 65 + EMA + regime + volume) makes entries very rare on 1d timeframe.",
      "Regime detection adds 40 daily bars (~6 weeks) of warmup before the first valid trade after a restart.",
      "ATR × 6 stop is wide on 1d — can mean 8-15% adverse move before the SL hits.",
      "Source backtest stats not provided; performance must be re-validated live.",
    ],
    params: {
      atr_length: 10,
      base_mult: 3.0,
      regime_lookback: 40,
      adx_length: 14,
      adx_threshold: 20,
      adaptive: true,
      trend_ema_length: 50,
      volume_ma_length: 20,
      min_signal_score: 65,
      sl_atr_mult: 6.0,
      tp_rr: 2.5,
      use_trail: false,
      cooldown_bars: 5,
    },
  },
  {
    id: 2,
    name: "rsi_momentum",
    symbol: "BTC",
    timeframe: "4h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/wZIdSrBG/",
    apr: "+24.3%*",
    sharpe: "1.85*",
    maxDrawdown: "14.8%*",
    winRate: "35.2%*",
    trades: "142 trades / 4 years*",
    summary: "Direct port of TradingView 'RSI > 70 Buy / Exit on Cross Below 70' by © Boubizee — counter-intuitive momentum continuation.",
    logic: [
      "RSI(14) on 4h candles. Long entry: RSI crosses ABOVE 70 (rsi > 70 AND prev_rsi <= 70).",
      "Long exit: RSI crosses BELOW 70 (rsi < 70 AND prev_rsi >= 70).",
      "No stop loss, no take profit, no filters — pure RSI threshold logic.",
      "Long-only, single position, 1× leverage to match source.",
      "* Backtest stats from Minara article (BTCUSDT 4h over ~4 years). Source PineScript itself works on any timeframe.",
    ],
    strengths: [
      "Verified byte-equivalent port of the original PineScript — no logic divergence.",
      "Per Minara backtest: best risk-adjusted profile (Sharpe 1.85, max DD 14.8%).",
      "Simple, fully mechanical — no discretionary judgment.",
      "Counter-intuitive thesis (RSI > 70 = strength, not exhaustion) means edge if you have the discipline to follow it.",
    ],
    weaknesses: [
      "~35% win rate (per article) — psychologically hard to keep trading after a streak of losses.",
      "No stop loss: a sustained reversal after entry has no protection.",
      "Edge depends on BTC's trend characteristics; weaker on range-bound assets.",
      "21% historical fee drag (per article) leaves a thin margin if you raise leverage.",
    ],
    params: { rsi_length: 14, rsi_level: 70 },
  },
  {
    id: 3,
    name: "bb_short",
    symbol: "SOL",
    timeframe: "1h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/UBGvlIlq/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h BB upper breakouts, short-only)",
    summary: "Direct port of TradingView 'BB Upper breakout Short +2%' by @DrZiuber — shorts intra-bar spikes above the upper Bollinger Band with a fixed limit take-profit.",
    logic: [
      "Bollinger Bands: basis = SMA(close, 20), upper = basis + 2.0 × stdev(close, 20).",
      "Entry trigger: bar's HIGH > upper × 1.02 (i.e. intra-bar spike at least 2% above the upper band).",
      "Opens SHORT at the trigger bar's close. No long side, no stop loss.",
      "Exit: limit order at entry × 0.98. The bar's low touching that level fills the trade exactly at the limit price.",
      "1× leverage to match source (fixed $10k cash on $40k initial capital ≈ 25% of equity).",
    ],
    strengths: [
      "Intra-bar HIGH trigger captures wick-driven spikes that close-based detection would miss.",
      "Limit-fill exit at the exact target gives predictable, repeatable per-trade PnL.",
      "Simple, mechanical mean-reversion logic with no discretionary judgment.",
      "No leverage means liquidation risk is minimal even in sustained adverse moves.",
    ],
    weaknesses: [
      "No stop loss: in a sustained trending breakout the position can sit underwater indefinitely.",
      "Mean reversion assumes the market eventually pulls back — fails in strong trending regimes.",
      "Only tested on SOL by the original author; behavior on other coins unverified.",
      "Source backtest stats not provided; performance must be re-validated live or via own backtest.",
    ],
    params: {
      bb_period: 20,
      bb_std: 2.0,
      breakout_pct: 0.02,
      take_profit_pct: 0.02,
      use_high: true,
    },
  },
  {
    id: 4,
    name: "sma_rsi",
    symbol: "ETH",
    timeframe: "1d",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/1x2AawHf/",
    apr: "+23.5%*",
    sharpe: "1.16*",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "19 trades*",
    summary: "Direct port of TradingView '50 & 200 SMA + RSI Average Strategy' by © muratkbesiroglu — classic trend following with a smoothed RSI filter.",
    logic: [
      "Long entry: close > SMA(50) AND close > SMA(200) AND SMA(RSI(21), 9) > 57.",
      "Long exit: close < SMA(50) AND SMA(RSI(21), 9) < 57.",
      "Long-only, single position, 1× leverage to match source.",
      "* Backtest stats from Minara article (ETHUSDT 1d). Source PineScript itself works on any timeframe.",
    ],
    strengths: [
      "Verified byte-equivalent port of the original PineScript — no logic divergence.",
      "Per Minara: beat ETH buy-and-hold by +117 percentage points during a period when holding ETH lost 22%.",
      "The smoothed RSI > 57 filter keeps the strategy in cash during weak-momentum chop, which is where most trend-followers bleed.",
      "Uses the oldest, most proven technical indicators — nothing exotic to overfit.",
    ],
    weaknesses: [
      "Requires 200 days of data warmup after a restart before SMA200 produces a valid signal.",
      "No stop loss: a sustained reversal between SMA50 cross and RSI confirmation has no protection.",
      "Trend-follower lag: in V-shaped recoveries the SMA200 condition delays entry significantly.",
      "Daily timeframe + strict filters → very few trades (19 in the article's window). Small sample size.",
    ],
    params: { sma_fast: 50, sma_slow: 200, rsi_length: 21, rsi_smooth: 9, rsi_threshold: 57 },
  },
  {
    id: 5,
    name: "volatility_breakout",
    symbol: "ETH",
    timeframe: "1h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/36zwwSMa/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h KC breakouts, long & short)",
    summary: "Direct port of TradingView 'Volume Breakout Strategy [Tables Fixed]' — Keltner Channel breakout with EMA trend, ADX, RSI and volume filters.",
    logic: [
      "Keltner Channels: basis = EMA(close, 22), bands = basis ± ATR(10) × 2.0.",
      "Long entry: close crosses ABOVE upper band AND volume > SMA(volume, 18) AND close > EMA(close, 220) AND RSI(14) > 50 AND ADX(14) > 20.",
      "Short entry: close crosses BELOW lower band AND volume > SMA(volume, 18) AND close < EMA(close, 220) AND RSI(14) < 50 AND ADX(14) > 20.",
      "Hard stop: entry ± ATR(14) × 4. Breakeven bump: SL moves to entry once price reaches +1.5% (long) / -1.5% (short).",
      "Trailing stop: activates at +3% / -3%, then trails 1% behind the highest high (long) or lowest low (short).",
      "4-hour cooldown between entries. Long & short. 2× leverage to match source.",
    ],
    strengths: [
      "Four independent filters (volume, EMA trend, RSI, ADX) gate every entry — drastically fewer false breakouts.",
      "Trailing stop captures extended trends instead of capping profit at a fixed target.",
      "Breakeven SL bump locks in zero-loss once a trade is +1.5%.",
      "Long & short — strategy works in both bullish and bearish regimes.",
    ],
    weaknesses: [
      "Strict filters mean trades are rare — long stretches with no entries are normal.",
      "Trailing stop with 1% offset can give back meaningful unrealized gain in a fast reversal.",
      "Source backtest stats not provided; performance must be validated live or via own backtest.",
      "ADX > 20 threshold is subjective — slightly weaker trends are excluded.",
    ],
    params: {
      kc_len: 22,
      kc_mult: 2.0,
      atr_kc_len: 10,
      ema_len: 220,
      adx_thresh: 20,
      vol_len: 18,
      rsi_len: 14,
      atr_sl_len: 14,
      sl_multiplier: 4.0,
      bk_activation_pct: 0.015,
      trail_start_pct: 0.03,
      trail_offset_pct: 0.01,
      cooldown_hours: 4,
    },
  },
  {
    id: 6,
    name: "btc_mean_reversion",
    symbol: "BTC",
    timeframe: "15m",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/pIrgsDpT/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (15m mean reversion, long & short)",
    summary: "Direct port of TradingView 'Optimized BTC Mean Reversion (RSI 20/65)' — RSI extremes confirmed by Stochastic, gated by EMA200 position.",
    logic: [
      "Long entry: RSI(14) < 20 AND raw fast %K(14) < 25 AND close > EMA(close, 200) × 0.9.",
      "Short entry: RSI(14) > 65 AND raw fast %K(14) > 75 AND close < EMA(close, 200).",
      "Long exit: SL = entry × 0.96, TP = entry × 1.06. Whichever the bar's low/high hits first.",
      "Short exit: SL = entry × 1.04, TP = entry × 0.94.",
      "Long & short, single position at a time, 1× leverage to match source's 100%-equity sizing.",
    ],
    strengths: [
      "Three-way confirmation (RSI extreme + Stochastic extreme + EMA200 position) filters out weak setups.",
      "Asymmetric RSI levels (20/65) require deeper oversold for entry but exit faster on recovery.",
      "Fixed SL/TP make per-trade risk and reward fully predictable.",
      "Trades both directions — works in trending and ranging regimes.",
    ],
    weaknesses: [
      "Mean-reversion strategies underperform during sustained one-directional moves (no trend-following).",
      "1.5:1 reward-to-risk (6% TP / 4% SL) requires win rate > 40% to be profitable after fees.",
      "Strict filters mean entries are very rare — expect long flat periods.",
      "EMA200 filter on 15m needs 50+ hours of data to warm up after a restart.",
    ],
    params: {
      ema_length: 200,
      rsi_period: 14,
      rsi_bull_level: 20,
      rsi_bear_level: 65,
      stoch_length: 14,
      stoch_oversold: 25,
      stoch_overbought: 75,
      stop_loss_pct: 0.04,
      take_profit_pct: 0.06,
    },
  },
  {
    id: 7,
    name: "hash_momentum",
    symbol: "SOL",
    timeframe: "4h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/L6VNlhiV/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (4h momentum, long & short)",
    summary: "Port of TradingView 'Hash Momentum Strategy' — normalized price momentum filtered by an ATR-dynamic threshold and EMA trend. Fixed-% SL with 2.5:1 RR take-profit.",
    logic: [
      "mom0 = close − close[13] (13-bar momentum). Normalized: mom0 / stdev(mom0, 39).",
      "Dynamic threshold: ATR(14) × 2.25. Momentum must exceed threshold to be tradeable.",
      "EMA(28) trend filter: longs only above EMA, shorts only below.",
      "Long: mom0 > threshold AND momentum accelerating (mom1 > 0) AND normMom > 0.5 AND close > close[1] AND close > EMA28.",
      "Short: mirror conditions below EMA28.",
      "SL = 2.2% from entry. TP = entry ± risk × 2.5 (1:2.5 RR). 6-bar cooldown after each trade.",
    ],
    strengths: [
      "Normalized momentum (σ units) makes the threshold meaningful across different volatility regimes.",
      "ATR-dynamic threshold automatically adjusts — strong signals in low-vol periods, fewer in high-vol.",
      "Multi-condition gate (momentum magnitude + acceleration + normalization + trend) rejects weak moves.",
      "Long & short with fixed RR gives defined risk on every trade.",
    ],
    weaknesses: [
      "No stop-loss on extreme gaps — slippage can cause fills beyond the 2.2% SL in illiquid conditions.",
      "Cooldown after close means a fast reversal immediately after exit is missed.",
      "39-bar stdev warmup plus ATR warmup requires ~50+ 4h bars before first valid signal.",
      "Source backtest stats not provided; performance must be validated live.",
    ],
    params: {
      mom_length: 13,
      mom_threshold_atr_mult: 2.25,
      ema_length: 28,
      stop_loss_pct: 2.2,
      risk_reward: 2.5,
      cooldown_bars: 6,
    },
  },
  {
    id: 8,
    name: "ema_crossover",
    symbol: "BTC",
    timeframe: "1h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/c0dAzn2Q/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h EMA crossover, long & short)",
    summary: "Port of TradingView '7/19 EMA Crypto Strategy' — fast/slow EMA crossover. Long on bullish cross, short on bearish. SL at the structural low/high of the last 4 candles.",
    logic: [
      "EMA(7) and EMA(19) on close.",
      "Long entry: EMA7 crosses above EMA19 (prev: ema7 ≤ ema19, cur: ema7 > ema19).",
      "Short entry: EMA7 crosses below EMA19.",
      "Stop loss: lowest low (long) / highest high (short) of the last 4 candles at signal bar.",
      "Exit: opposite cross (reversal) or SL hit. No fixed take-profit — ride the trend.",
      "Long & short. Reverses directly on opposite cross without waiting for flat.",
    ],
    strengths: [
      "Structural SL anchors the stop to recent price action — adapts to volatility at time of entry.",
      "Direct reversal on opposite cross means the strategy is always aligned with the current trend direction.",
      "Simple and fully mechanical — no discretionary judgment.",
      "EMA7/19 responds quickly to intraday trend changes on 1h.",
    ],
    weaknesses: [
      "Frequent false crosses in sideways markets generate repeated small losses.",
      "SL at N-candle structure can be very tight in low-volatility regimes, causing premature stops.",
      "No take-profit means profits fully depend on sustained trend moves post-entry.",
      "Source backtest stats not provided; performance depends heavily on market regime.",
    ],
    params: { fast_len: 7, slow_len: 19, sl_candles: 4 },
  },
  {
    id: 9,
    name: "cdc_macd",
    symbol: "SOL",
    timeframe: "1d",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/7nv3hTpO/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1d EMA crossover, long only)",
    summary: "Port of TradingView 'CDC Backtest (MACD)' — EMA 12/26 crossover long-only. Mathematically equivalent to MACD crossing zero. Simple, no SL or TP.",
    logic: [
      "EMA(12) and EMA(26) on daily close.",
      "Long entry: EMA12 crosses above EMA26 (equivalent: MACD > 0 fresh cross).",
      "Long exit: EMA12 crosses below EMA26.",
      "Long-only, no stop loss, no take profit. Full equity sizing.",
      "Source default_qty_value = $200k per trade on $400k capital (50% allocation).",
    ],
    strengths: [
      "One of the oldest, most battle-tested trend-following signals in technical analysis.",
      "Long-only on daily avoids short-selling complexity and overnight margin risk.",
      "Clean, unambiguous entry/exit rules — no parameter tuning needed beyond EMA lengths.",
      "Daily timeframe means few trades and low transaction costs.",
    ],
    weaknesses: [
      "No stop loss: an adverse move after entry has no protection until the cross reverses.",
      "Slow to react — EMA12/26 crossover on 1d can lag price by several days.",
      "Long-only misses profitable short setups in downtrends.",
      "Mathematically equivalent to MACD zero-line cross — not independently diversifying vs. macd_zero.",
    ],
    params: { ema_fast: 12, ema_slow: 26 },
  },
  {
    id: 10,
    name: "keltner_breakout",
    symbol: "ETH",
    timeframe: "4h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/LmNV3ZLN/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (4h KC breakout, long only)",
    summary: "Port of TradingView 'ETHUSDT 4H - Keltner Breakout (Pro)' — enters long when close breaks above the upper Keltner Channel while above EMA(200). ATR-based SL, 20% TP, KC-lower exit.",
    logic: [
      "EMA(200) trend filter: only long when close > EMA200.",
      "Keltner Channel: basis = EMA(close, 20), upper = basis + ATR(14) × 2.0.",
      "Entry: close > upper KC AND close > EMA200.",
      "Stop loss: entry − ATR(14) × 4.0 (wide stop for 4h volatility).",
      "Take profit: entry × 1.20 (20% target).",
      "Additional exit: close drops below lower KC (trend weakness signal).",
    ],
    strengths: [
      "EMA200 filter ensures entries are only in the long-term bullish trend direction.",
      "KC breakout captures genuine momentum moves, not just noise.",
      "Three exit paths (SL / TP / KC lower) adapt to different market conditions.",
      "Source was designed specifically for ETHUSDT 4H — symbol/timeframe validated by author.",
    ],
    weaknesses: [
      "Long-only misses short-side opportunities.",
      "EMA200 on 4h needs 800+ hours of data (~33 days) to warm up after restart.",
      "ATR×4 stop on 4h can be 5-10% below entry — large per-trade risk in volatile periods.",
      "20% TP is ambitious — many breakouts reverse before reaching the target.",
    ],
    params: { ema_len: 200, kc_len: 20, atr_len: 14, kc_mult: 2.0, sl_atr_mult: 4.0, tp_pct: 0.20 },
  },
  {
    id: 11,
    name: "pivot_supertrend",
    symbol: "BTC",
    timeframe: "4h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/b74KzneI/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (4h pivot ST, long & short)",
    summary: "Port of TradingView 'Pivot Point SuperTrend' by Kadunagra — uses pivot highs/lows to anchor a dynamic center line, then builds a SuperTrend trailing stop from it. EMA200 trend filter. 1% SL.",
    logic: [
      "Pivot highs/lows detected with 2-bar left/right confirmation.",
      "Center line: exponentially-weighted average of consecutive pivot levels: center = (center × 2 + lastPivot) / 3.",
      "SuperTrend bands: Up = center − 3 × ATR(10), Dn = center + 3 × ATR(10).",
      "TUp (support): ratchets up — only moves higher, never lower. TDown (resistance): only moves lower.",
      "Trend = 1 (bullish) when close > prev TDown, −1 when close < prev TUp.",
      "Long signal: trend flips 1 AND close > EMA(200). Short: flips −1 AND close < EMA(200). SL = 1%.",
    ],
    strengths: [
      "Pivot-anchored center line reduces noise vs. price-anchored SuperTrend — more meaningful flip signals.",
      "EMA200 filter keeps the strategy aligned with the macro trend.",
      "Ratcheting TUp/TDown gives the trailing stop a natural anti-whipsaw property.",
      "Long & short with tight 1% SL gives very defined risk per trade.",
    ],
    weaknesses: [
      "2-bar pivot confirmation introduces a 2-bar lag — signals are slightly delayed.",
      "EMA200 on 4h requires 800+ hours warmup; pivot detection needs additional bars.",
      "1% SL is very tight on 4h — normal volatility can stop out valid trades.",
      "Source backtest stats not provided; performance must be validated live.",
    ],
    params: { ma_len: 200, sl_pct: 1.0 },
  },
  {
    id: 12,
    name: "macd_zero",
    symbol: "BTC",
    timeframe: "1d",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/llTXO45e/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1d MACD zero-line, long only)",
    summary: "Port of TradingView 'BTC MACD Zero-Line Strategy (Long Only)' — enters long when the MACD line crosses above zero, exits when it crosses below. No SL or TP.",
    logic: [
      "MACD = EMA(12) − EMA(26). Signal = EMA(9) of MACD. Histogram = MACD − Signal.",
      "Long entry: MACD crosses above 0 (prev_macd ≤ 0 AND cur_macd > 0).",
      "Long exit: MACD crosses below 0 (prev_macd ≥ 0 AND cur_macd < 0).",
      "Long-only, no stop loss, no take profit. Full equity sizing.",
      "Source designed for BTC daily chart.",
    ],
    strengths: [
      "Zero-line cross is a cleaner signal than signal-line cross — fewer false positives.",
      "Daily timeframe means few trades, low churn, and meaningful trend signals.",
      "Long-only on BTC aligns with the asset's historical long-term upward bias.",
      "Mathematically simple and robust — no tuning beyond MACD parameters.",
    ],
    weaknesses: [
      "No stop loss: a sustained reversal after entry rides out with no protection.",
      "MACD zero-line cross on 1d can lag by many days during fast reversals.",
      "Long-only misses bear market short opportunities.",
      "Highly correlated with cdc_macd (also EMA12/26 crossover, long only) — limited diversification.",
    ],
    params: { macd_fast: 12, macd_slow: 26, macd_signal: 9 },
  },
  {
    id: 13,
    name: "moon_phases",
    symbol: "BTC",
    timeframe: "1d",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/sl42otOB/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1d lunar cycle, long only, experimental)",
    summary: "Port of TradingView 'Moon Phases Long/Short Strategy' — enters long at the start of a full moon and exits at the new moon. 5% SL / 10% TP. Experimental / research use.",
    logic: [
      "Lunar day computed from timestamp: daysSince = (bar_time − 2000-01-06T00:00Z) / 86400 days. lunarDay = floor((daysSince mod 29.53) + 0.5).",
      "Full moon: lunarDay 13–15. New moon: lunarDay 0–1.",
      "Long entry: first bar where full moon starts (lunarDay in [13,14,15] AND prev was not).",
      "Long exit: new moon starts (lunarDay in [0,1] AND prev was not) OR SL hit (5%) OR TP hit (10%).",
      "Long-only. Default 'Long Only (Research)' mode from source.",
    ],
    strengths: [
      "Completely uncorrelated with all other technical strategies — novel diversification source.",
      "Clear entry/exit rules anchored to a deterministic calendar cycle.",
      "Fixed SL/TP bound per-trade losses and capture moonshot gains.",
      "Generates ~13 trade opportunities per year (one per lunar cycle).",
    ],
    weaknesses: [
      "No empirical edge is established in crypto — performance is hypothesis-driven.",
      "Lunar cycle is independent of market structure, news, or fundamentals.",
      "Only 13 signals/year makes statistical validation extremely slow.",
      "Primarily for research/diversification; not a core alpha strategy.",
    ],
    params: { stop_loss_pct: 5.0, take_profit_pct: 10.0 },
  },
  {
    id: 14,
    name: "penguin_volatility",
    symbol: "ETH",
    timeframe: "1h",
    enabled: true,
    tvUrl: "https://www.tradingview.com/script/skzo4i9e/",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h volatility state, long only)",
    summary: "Port of TradingView 'Penguin Volatility State Strategy' (© waranyu.trkm) — classifies market state using BB/KC width ratio, then enters long when volatility accelerates in a bullish EMA state.",
    logic: [
      "Bollinger Bands (SMA20, 2σ) and Keltner Channel (SMA20, ATR20 × 2.0) computed on same basis.",
      "diff = (upperBB − upperKC) / upperKC × 100 — positive when BB wider than KC (volatility expanding).",
      "RSI applied to the diff series (RSI-of-diff, length 14) + SMA(7) smoothed = rsi_diff2.",
      "EMA state: Green (fast>slow AND price momentum bullish) or Yellow (fast>slow AND momentum weakening).",
      "Long entry (timing filter): rsi_diff2 crosses UNDER rsi_diff (volatility accelerating) AND state is Green or Yellow.",
      "Long exit: rsi_diff crosses UNDER rsi_diff2 (volatility decelerating). No SL/TP.",
    ],
    strengths: [
      "RSI-of-diff is a second-derivative volatility signal — catches the start of vol expansions, not just the expansion itself.",
      "EMA state filter prevents entries in bearish macro conditions.",
      "Timing filter reduces overtrading vs. the default always-in-trend approach.",
      "Unique signal source (BB/KC width dynamics) — diversifies from price-based strategies.",
    ],
    weaknesses: [
      "No stop loss: a volatility spike in the wrong direction rides until rsi_diff decelerates.",
      "Complex indicator chain (BB → KC → diff → RSI → SMA) introduces significant lag.",
      "EMA state can flip rapidly on 1h, causing frequent position churn.",
      "Source backtest stats not provided; performance must be validated live.",
    ],
    params: {
      bb_len: 20,
      bb_mult: 2.0,
      kc_mult: 2.0,
      ema_fast_len: 12,
      ema_slow_len: 26,
      rsi_diff_len: 14,
      rsi_avg_len: 7,
    },
  },
  {
    id: 15,
    name: "daily_long_0830",
    symbol: "BTC",
    timeframe: "15m",
    enabled: true,
    tvUrl: "",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Time-of-day long: enter 08:30 UTC, exit 08:00 UTC next day",
    summary: "Pure time-based long-only strategy. Enters at exactly 08:30 UTC (first 15m candle whose minute matches), exits at 08:00 UTC the following morning. No price/indicator logic — just clock-driven.",
    logic: [
      "Compares the latest closed candle's UTC timestamp.",
      "Enters long when minute == 30 and hour == 8.",
      "Exits long when minute == 0 and hour == 8.",
      "No SL, no TP, no filters.",
    ],
    strengths: [
      "Trivially simple — no indicator drift, no warmup.",
      "Captures any consistent time-of-day price drift if it exists.",
      "Predictable hold period (~23.5h).",
    ],
    weaknesses: [
      "No edge unless the chosen time window genuinely correlates with positive return.",
      "No risk management — full overnight exposure.",
      "Backtest shows -2.5% APR over 44d sample — not statistically significant either way.",
    ],
    params: {},
  },
  {
    id: 16,
    name: "kalman_breakout",
    symbol: "ETH",
    timeframe: "1h",
    enabled: true,
    tvUrl: "",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h Kalman bands, long & short)",
    summary: "Port of 'Kinetic Kalman Breakout' — 2-state Kalman filter (position + velocity) on close prices produces a smoothed mean. Bands = mean ± 2.6 × MAE. Trades on close cross of bands.",
    logic: [
      "Kalman filter recomputed each tick over full closed-bar history.",
      "absDiff = |close − kalmanPrice|, then SMA(absDiff, 200) = MAE.",
      "Upper/Lower band = kalmanPrice ± 2.6 × MAE.",
      "Long on close crossover upper band, short on close crossunder lower band.",
      "Reverses on opposite signal via engine flip-detect.",
    ],
    strengths: [
      "Adaptive smoothing — Kalman tracks both level and velocity, more responsive than EMAs in trends.",
      "Bands widen with realized volatility (MAE-based), narrowing in calm regimes.",
      "Long & short — works in both trend directions.",
    ],
    weaknesses: [
      "180d backtest: -3.8% APR, 27% win rate. Few signals (15 round trips) but mostly losing in this regime.",
      "No SL — relies entirely on flip on opposite signal.",
      "Kalman filter recomputed each tick (O(n) per call) — fine live, slow on huge backtest windows.",
    ],
    params: { process_noise_pos: 0.05, process_noise_vel: 0.0001, measurement_noise: 250, band_lookback: 200, band_multiplier: 2.6 },
  },
  {
    id: 17,
    name: "bb_rsi_scalper",
    symbol: "BTC",
    timeframe: "15m",
    enabled: true,
    tvUrl: "",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (15m scalper, long only)",
    summary: "Port of 'Crypto LONG 10m Scalper BB+RSI+EMA+Fib' — long-only multi-filter scalper. Originally 10m on TradingView; runs on 15m here since HyperLiquid doesn't expose a 10m timeframe.",
    logic: [
      "Bollinger Bands (20, 2σ), RSI(14), EMA(9), Fibonacci levels — all coincide for entry.",
      "Long entry: RSI < 20 + price breaks BB lower + price in Fibonacci zone + EMA turns up.",
      "Exit: technical reversal OR time-out, gated on having 0.1% profit.",
      "No SL — only exits when in profit.",
    ],
    strengths: [
      "Multi-filter entry suppresses false signals.",
      "Fibonacci confluence is a popular discretionary mark — encoding it removes hesitation.",
      "180d backtest: ~0% APR, 57% win rate, 7 trades — break-even but consistent.",
    ],
    weaknesses: [
      "No stop loss — a sustained downtrend post-entry can hold the position indefinitely.",
      "Pyramiding=1 in source means one position at a time (we already enforce this engine-side).",
    ],
    params: { bb_length: 20, bb_mult: 2.0, rsi_length: 14, ema_length: 9 },
  },
  {
    id: 18,
    name: "hash_supertrend",
    symbol: "BTC",
    timeframe: "1h",
    enabled: true,
    tvUrl: "",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h SuperTrend flips, long & short)",
    summary: "Port of 'Hash Supertrend' by Hash Capital Research — pure SuperTrend(16, 3.11) flip strategy. Long on bullish flip, short on bearish flip, reverses every cross.",
    logic: [
      "ATR(16) with multiplier 3.11.",
      "SuperTrend bands: hl2 ± multiplier × ATR with continuity logic.",
      "Long entry on bullish flip (direction goes from bearish to bullish).",
      "Short entry on bearish flip — engine flip-detect handles the reverse.",
      "No stop loss, no take profit, no other filters.",
    ],
    strengths: [
      "Simple and reactive — never far from the prevailing trend.",
      "Long & short — captures both up and down moves.",
    ],
    weaknesses: [
      "No SL — risk control depends entirely on engine's MAX_POSITION_SIZE_USD cap.",
      "180d backtest: -6.9% APR, 33% win rate, 96 trades — over-trades in choppy regimes.",
      "Funding cost on 96 round-trips adds up — strategy should be paired with funding-aware analysis.",
    ],
    params: { atr_period: 16, factor: 3.11 },
  },
  {
    id: 19,
    name: "oleg_aryukov",
    symbol: "ETH",
    timeframe: "1h",
    enabled: true,
    tvUrl: "",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h ensemble, long & short, EMA50/200 trend-filter)",
    summary: "Port of 'Oleg_Aryukov_Strategy' — 6-indicator ensemble with voting. Each of RSI / Williams %R / TSI / KDJ / %BB / Nadaraya-Watson casts a vote; trade when ≥3 confirm AND EMA50/200 trend filter aligns.",
    logic: [
      "6 indicators vote independently for long or short bias.",
      "EMA50/200 trend filter gates direction (no shorts above EMA200, no longs below).",
      "Fixed % SL and TP, plus a percentage trailing stop.",
      "Nadaraya-Watson is a Gaussian-kernel weighted regression smoother (custom hand-roll).",
    ],
    strengths: [
      "Ensemble voting reduces dependence on any single indicator's quirks.",
      "Trend filter prevents counter-trend trades in strong directional regimes.",
      "Both long and short — works in both market modes.",
    ],
    weaknesses: [
      "Nadaraya-Watson + RCI loops are O(n²) — fine live (1 call per tick), too slow for 4k-bar backtests.",
      "Tight trailing stop (1%) at default — many quick stop-outs.",
      "Russian source comments — translation may lose nuance.",
    ],
    params: { rsi_length: 12, williams_length_6: 6, williams_length_12: 12 },
  },
  {
    id: 20,
    name: "qullamagi_breakout",
    symbol: "ETH",
    timeframe: "1h",
    enabled: true,
    tvUrl: "",
    apr: "N/A",
    sharpe: "N/A",
    maxDrawdown: "N/A",
    winRate: "N/A",
    trades: "Active (1h breakout, long & short)",
    summary: "Port of 'Qullamaggie MA Breakout' (Loose Intraday preset) — multi-MA trend-stack breakout with ADX/RSI filters, intra-bar box breakout, EMA-trail stop with breakeven activation.",
    logic: [
      "MA stack: MA1 > MA2 > MA3 must align with direction.",
      "ADX and RSI filters confirm momentum.",
      "Intra-bar box breakout is the trigger.",
      "EMA-trail stop with buffer + hard SL + breakeven activation.",
      "Wide-candle and volume filters (1.1×).",
      "Short-only consec-red filter, cooldown between trades.",
    ],
    strengths: [
      "Many filters → high-quality entries when they fire.",
      "Trail-stop locks in profit on extended moves.",
      "Long & short with hard risk control.",
    ],
    weaknesses: [
      "180d backtest: -6.9% APR, 23% win rate — over-trades in this regime.",
      "Scale-out partial close not emitted (engine doesn't support fractional close yet).",
      "Strict (Daily) preset not ported — only Loose (Intraday).",
    ],
    params: { preset: "Loose (Intraday)" },
  },
  {
    id: 21,
    name: "vvv_hedge",
    symbol: "VVV",
    timeframe: "4h",
    enabled: true,
    tvUrl: "",
    apr: "+2.65%*",
    sharpe: "0.38*",
    maxDrawdown: "6.30%*",
    winRate: "50%*",
    trades: "2 round trips / 144d (defensive — designed to fire rarely)",
    summary: "Custom defensive hedge for staked VVV holdings. Shorts 400 VVV when long-term uptrend breaks; closes when EMA flips back bullish. NOT a Pine port — designed in-house specifically for this use case. *backtest 2025-12-08 → 2026-05-01 with EMA filter on.",
    logic: [
      "EMA21 < EMA55 + close < EMA55 is a MANDATORY filter (must be true to even consider a short).",
      "When EMA confirms, requires 2-of-3 additional bearish votes from: ATR(14) chandelier exit, RSI(14) bearish divergence, volume distribution (7d > 1.5× 30d).",
      "Open emits Signal(size=400) — bypasses engine notional calc, exact 1:1 hedge of the spot holding.",
      "Hard SL: 10% above entry (~20% margin loss at 2× — survivable, last-resort liquidation guard).",
      "Symmetric exit: as soon as EMA flips back bullish, close immediately. Don't wait for full vote turnaround — re-engagement with spot trend matters more for a hedge.",
    ],
    strengths: [
      "EMA filter eliminated 3 of 5 false signals during the audit-rerun on the last 6 months of VVV uptrend.",
      "Size invariant: every emitted signal is exactly holding_vvv (default 400). Never under- or over-hedges.",
      "2× leverage capped — even a +10% gap up only loses 20% of margin, not the position.",
      "Auto-closes on EMA bullish flip — won't hedge into a fresh leg up.",
    ],
    weaknesses: [
      "Lagging by design — accepts ~15-20% of a top before triggering, in exchange for many fewer false alarms.",
      "Backtested only on the up-cycle so far. VVV has 14 months of history but no prior major top to learn from. First real top will be the first true validation.",
      "If VVV spikes >10% above entry before EMA flips bullish, hard SL fires and the hedge unwinds at a loss. Rare but possible.",
    ],
    params: { holding_vvv: 400, ema_fast_len: 21, ema_slow_len: 55, atr_len: 14, chandelier_lookback: 30, chandelier_atr_mult: 3.0, rsi_len: 14, rsi_div_window: 20, hard_sl_pct: 0.10, require_ema_bearish: true, bearish_votes_to_open: 2, bearish_votes_to_keep: 1 },
  },
];

export default function StrategiesPage() {
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Strategies</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Click a strategy below to jump to its details. Leverage and on/off
          controls live on the{" "}
          <a href="/status" className="underline hover:text-foreground">
            status page
          </a>
          .
        </p>
      </div>

      {/* Top menu: one card per strategy with basic info, anchor-linked */}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {strategies.map((s) => (
          <a
            key={s.id}
            href={`#${s.name}`}
            className="block rounded-lg border bg-card p-4 transition hover:border-foreground/40 hover:bg-accent/50"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="font-mono font-semibold text-sm">{s.name}</div>
              <Badge variant="outline" className="font-mono text-[10px] shrink-0">
                {s.symbol} {s.timeframe}
              </Badge>
            </div>
            <p className="mt-2 text-xs text-muted-foreground line-clamp-3">
              {s.summary}
            </p>
            <div className="mt-3 flex items-center gap-3 text-[11px]">
              <span className="text-muted-foreground">APR</span>
              <span className="font-mono text-green-400">{s.apr}</span>
              <span className="text-muted-foreground">Sharpe</span>
              <span className="font-mono">{s.sharpe}</span>
              <span className="text-muted-foreground">DD</span>
              <span className="font-mono text-red-400">{s.maxDrawdown}</span>
            </div>
          </a>
        ))}
      </div>

      <Separator />

      {/* Detail cards — anchor targets */}
      {strategies.map((s) => (
        <Card key={s.id} id={s.name} className="scroll-mt-6">
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-xl font-mono">{s.name}</CardTitle>
              <Badge variant="outline" className="font-mono">
                {s.symbol} {s.timeframe}
              </Badge>
            </div>
            <p className="text-muted-foreground">{s.summary}</p>
          </CardHeader>
          <CardContent className="space-y-6">
            {/* Stats row */}
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
              <div>
                <p className="text-xs text-muted-foreground">APR</p>
                <p className="text-lg font-bold text-green-400">{s.apr}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Sharpe</p>
                <p className="text-lg font-bold">{s.sharpe}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Max Drawdown</p>
                <p className="text-lg font-bold text-red-400">{s.maxDrawdown}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Win Rate</p>
                <p className="text-lg font-bold">{s.winRate}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">Trades</p>
                <p className="text-lg font-bold">{s.trades}</p>
              </div>
            </div>

            <Separator />

            {/* How it works */}
            <div>
              <h3 className="mb-2 font-semibold">How it works</h3>
              <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
                {s.logic.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </div>

            {/* Strengths & Weaknesses */}
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <h3 className="mb-2 font-semibold text-green-400">Strengths</h3>
                <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                  {s.strengths.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              </div>
              <div>
                <h3 className="mb-2 font-semibold text-red-400">Weaknesses</h3>
                <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
                  {s.weaknesses.map((item, i) => (
                    <li key={i}>{item}</li>
                  ))}
                </ul>
              </div>
            </div>

            <Separator />

            {/* Parameters */}
            <div>
              <h3 className="mb-2 font-semibold">Parameters</h3>
              <div className="flex flex-wrap gap-2">
                {Object.entries(s.params).map(([key, value]) => (
                  <Badge key={key} variant="outline" className="font-mono text-xs">
                    {key}: {value}
                  </Badge>
                ))}
              </div>
            </div>

            {/* TradingView chart with strategy indicators */}
            {s.enabled && (
              <>
                <Separator />
                <div>
                  <h3 className="mb-2 font-semibold">
                    Live Chart with Indicators
                  </h3>
                  <TradingViewChart
                    symbol={s.symbol}
                    timeframe={s.timeframe}
                    height={450}
                    strategy={s.name}
                  />
                </div>
              </>
            )}

            <div className="flex items-center justify-between pt-2">
              <a
                href={s.tvUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-muted-foreground underline hover:text-foreground"
              >
                View on TradingView ↗
              </a>
              <a
                href="#"
                className="text-xs text-muted-foreground underline hover:text-foreground"
              >
                ↑ Back to top
              </a>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
