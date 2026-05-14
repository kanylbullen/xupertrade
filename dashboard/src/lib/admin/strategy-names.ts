/**
 * Mirror of the bot's registered strategies. Used by the admin
 * allowlist PATCH validator to reject typos before they hit the DB.
 * If a new strategy is added to `bot/hypertrade/strategies/registry.py:load_all`,
 * add the name here.
 */

export const KNOWN_STRATEGIES = [
  "supertrend",
  "rsi_momentum",
  "bb_short",
  "sma_rsi",
  "volatility_breakout",
  "btc_mean_reversion",
  "hash_momentum",
  "ema_crossover",
  "cdc_macd",
  "keltner_breakout",
  "pivot_supertrend",
  "macd_zero",
  "moon_phases",
  "penguin_volatility",
  "daily_long_0830",
  "kalman_breakout",
  "bb_rsi_scalper",
  "hash_supertrend",
  "oleg_aryukov",
  "qullamagi_breakout",
  "vvv_hedge",
  "ath_breakout",
] as const;
