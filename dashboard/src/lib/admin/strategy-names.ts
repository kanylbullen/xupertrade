/**
 * Mirror of the bot's registered strategies. Used by the admin
 * allowlist PATCH validator to reject typos before they hit the DB,
 * and by the mainnet-allowlist UI to render a row per strategy with
 * a brief description.
 *
 * If a new strategy is added to `bot/hypertrade/strategies/registry.py:load_all`,
 * add it to STRATEGIES below — KNOWN_STRATEGIES is derived from it.
 */

export type StrategyMeta = {
  name: string;
  symbol: string;
  timeframe: string;
  summary: string;
};

export const STRATEGIES: readonly StrategyMeta[] = [
  { name: "supertrend",          symbol: "BTC", timeframe: "1d", summary: "Adaptive SuperTrend with AI signal score; long & short, SL/TP." },
  { name: "rsi_momentum",        symbol: "BTC", timeframe: "4h", summary: "Counter-intuitive: long when RSI(14) crosses above 70; exit below." },
  { name: "bb_short",            symbol: "SOL", timeframe: "1h", summary: "Short intra-bar spikes 2% above the upper Bollinger band; limit-fill exit." },
  { name: "sma_rsi",             symbol: "BTC", timeframe: "1h", summary: "SMA + RSI trend continuation; long-only with SL." },
  { name: "volatility_breakout", symbol: "BTC", timeframe: "1h", summary: "ATR-channel breakout long; SL recomputed every bar from current ATR." },
  { name: "btc_mean_reversion",  symbol: "BTC", timeframe: "1h", summary: "Mean-reversion long on z-score extremes; structured SL/TP." },
  { name: "hash_momentum",       symbol: "SOL", timeframe: "1h", summary: "Hash-ribbon-style momentum; flip on opposite signal." },
  { name: "ema_crossover",       symbol: "BTC", timeframe: "1h", summary: "Classic EMA crossover; exit on SL only (no reversal)." },
  { name: "cdc_macd",            symbol: "BTC", timeframe: "1d", summary: "CDC ActionZone + MACD trend filter; long-only." },
  { name: "keltner_breakout",    symbol: "BTC", timeframe: "1h", summary: "Keltner-channel breakout with persistent SL across restarts." },
  { name: "pivot_supertrend",    symbol: "ETH", timeframe: "1h", summary: "Pivot-anchored SuperTrend; flips on band reversal." },
  { name: "macd_zero",           symbol: "BTC", timeframe: "1d", summary: "MACD zero-line cross; long-only trend follower." },
  { name: "moon_phases",         symbol: "BTC", timeframe: "1d", summary: "Lunar-cycle calendar long; deterministic dates." },
  { name: "penguin_volatility",  symbol: "ETH", timeframe: "1h", summary: "ATR-based volatility regime entries; state-driven, no timing filter by default." },
  { name: "daily_long_0830",     symbol: "BTC", timeframe: "15m", summary: "Time-of-day long at 08:30 UTC; flat by session close." },
  { name: "kalman_breakout",     symbol: "ETH", timeframe: "1h", summary: "Kalman-filter mean + ATR bands; trend-following breakouts." },
  { name: "bb_rsi_scalper",      symbol: "BTC", timeframe: "15m", summary: "Bollinger + RSI + EMA + Fib scalper; long/short with tight SL." },
  { name: "hash_supertrend",     symbol: "BTC", timeframe: "1h", summary: "SuperTrend flip without SL; pure trend-following." },
  { name: "oleg_aryukov",        symbol: "ETH", timeframe: "1h", summary: "6-indicator ensemble (NW kernel, RCI, ...); high-conviction long." },
  { name: "qullamagi_breakout",  symbol: "ETH", timeframe: "1h", summary: "Multi-MA breakout per Qullamaggie; structured risk." },
  { name: "vvv_hedge",           symbol: "VVV", timeframe: "1h", summary: "Defensive hedge for staked VVV holdings; EMA-bearish mandatory filter." },
  { name: "ath_breakout",        symbol: "BTC", timeframe: "1d", summary: "All-time-high breakout long; trail-stop on new highs." },
] as const;

export const KNOWN_STRATEGIES: ReadonlyArray<string> = STRATEGIES.map(
  (s) => s.name,
);

/**
 * Operator-cap parsed from `HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES`
 * (same env the orchestrator passes to bots). Empty/missing = empty
 * set = fail-closed (no mainnet trading possible).
 *
 * Single source of truth so the dashboard GET route, the per-strategy
 * POST validator, and tests all agree on what "operator allows" means.
 */
export function getMainnetOperatorCap(): Set<string> {
  const raw = process.env.HYPERTRADE_BOT_MAINNET_ENABLED_STRATEGIES ?? "";
  return new Set(
    raw
      .split(",")
      .map((n) => n.trim())
      .filter((n) => n.length > 0 && KNOWN_STRATEGIES.includes(n)),
  );
}
