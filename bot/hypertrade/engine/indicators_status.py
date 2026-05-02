"""Compute current indicator state for each strategy — used to show 'how close to trigger'."""

from dataclasses import dataclass

import pandas as pd
from hypertrade.data.feed import fetch_candles
from hypertrade.data.indicators import bollinger_bands, rsi, sma, supertrend
from hypertrade.db.repo import Repository


@dataclass
class StrategyStatus:
    name: str
    symbol: str
    timeframe: str
    price: float
    signal: str  # "flat" | "long" | "short" | "ready_long" | "ready_short"
    distance_pct: float  # how far from trigger (0 = ready, positive = distance remaining)
    details: dict  # strategy-specific indicator values
    description: str  # human-readable trigger condition
    has_open_position: bool = False
    position_side: str = ""  # "long" | "short" | ""


async def _has_open_position(repo: Repository | None, strategy: str, symbol: str) -> tuple[bool, str]:
    """Returns (has_position, side)."""
    if repo is None:
        return False, ""
    try:
        pos = await repo.get_open_position(strategy, symbol)
        if pos:
            return True, pos.side
    except Exception:
        pass
    return False, ""


async def _supertrend_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "1d") -> StrategyStatus:
    """Status mirrors the new adaptive SuperTrend strategy."""
    from hypertrade.strategies.supertrend import SuperTrendStrategy

    df = await fetch_candles(symbol, timeframe, limit=300)
    strat = SuperTrendStrategy()
    df = strat._compute_adaptive_supertrend(df.copy())
    import pandas_ta as pta
    df["trend_ema"] = pta.ema(df["close"], length=strat.trend_ema_length)
    df["vol_ma"] = pta.sma(df["volume"], length=strat.volume_ma_length)

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]

    price = float(latest["close"])
    st = float(latest["st_band"])
    direction = int(latest["st_dir"])
    prev_direction = int(prev["st_dir"])
    regime = int(latest["regime"])
    regime_label = ["ranging", "trending", "volatile"][regime]
    adapt_mult = float(latest["adapt_mult"])
    adx_val = float(latest["adx"])
    trend_ema = float(latest["trend_ema"])
    vol = float(latest["volume"])
    vol_ma = float(latest["vol_ma"])

    distance = abs(price - st) / price * 100
    has_pos, pos_side = await _has_open_position(repo, "supertrend", symbol)

    if has_pos:
        signal = pos_side
        desc = (
            f"Holding {pos_side} {symbol}. ATR×6 stop, {strat.tp_rr}:1 RR take-profit. "
            f"Regime: {regime_label}, ST band ${st:,.0f}."
        )
    elif direction != prev_direction:
        # A flip occurred — but the strategy may still reject it via score/filters.
        is_bull = direction == 1
        score = strat._score_signal(df, is_bull)
        passes_score = score >= strat.min_signal_score
        passes_trend = (not strat.require_trend_alignment) or (
            (is_bull and price > trend_ema) or (not is_bull and price < trend_ema)
        )
        passes_regime = (not strat.skip_ranging) or regime != 0
        passes_vol = (not strat.require_volume_spike) or vol > vol_ma

        if passes_score and passes_trend and passes_regime and passes_vol:
            signal = "ready_long" if is_bull else "ready_short"
            desc = (
                f"ST flipped {'bull' if is_bull else 'bear'} this candle — score {score}/100 "
                f"passes all filters. Fires next tick."
            )
        else:
            signal = "flat"
            blockers = []
            if not passes_score:
                blockers.append(f"score {score}<{strat.min_signal_score}")
            if not passes_trend:
                blockers.append(f"price vs EMA{strat.trend_ema_length} misaligned (price ${price:,.0f}, EMA ${trend_ema:,.0f})")
            if not passes_regime:
                blockers.append("regime is ranging")
            if not passes_vol:
                blockers.append(f"volume {vol:,.0f}<avg {vol_ma:,.0f}")
            desc = (
                f"ST flipped {'bull' if is_bull else 'bear'} but entry rejected. "
                f"Score {score}/100. Blocked by: {', '.join(blockers)}."
            )
    else:
        signal = "flat"
        trend_word = "bullish" if direction == 1 else "bearish"
        flip_dir = "below" if direction == 1 else "above"
        desc = (
            f"ST trend {trend_word} (band ${st:,.0f}, regime {regime_label}, "
            f"adapt mult {adapt_mult:.2f}, ADX {adx_val:.0f}). "
            f"Waiting for flip: close needs to go {flip_dir} ${st:,.0f}."
        )

    return StrategyStatus(
        name="supertrend",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=distance,
        details={
            "close": price,
            "st_band": st,
            "st_direction": direction,
            "regime": regime_label,
            "adapt_mult": adapt_mult,
            "adx_14": adx_val,
            "trend_ema_50": trend_ema,
            "volume": vol,
            "volume_avg_20": vol_ma,
        },
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _rsi_momentum_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "4h") -> StrategyStatus:
    df = await fetch_candles(symbol, timeframe, limit=30)
    df = rsi(df.copy(), 14)
    closed = df.iloc[:-1]
    price = float(closed["close"].iloc[-1])
    cur_rsi = float(closed["rsi"].iloc[-1])
    prev_rsi = float(closed["rsi"].iloc[-2])
    has_pos, pos_side = await _has_open_position(repo, "rsi_momentum", symbol)

    if has_pos:
        signal = pos_side
        desc = f"Holding {pos_side}. Will close when RSI crosses below 70 (current {cur_rsi:.1f})"
    elif prev_rsi <= 70 and cur_rsi > 70:
        signal = "ready_long"
        distance = 0.0
        desc = f"RSI just crossed above 70 ({prev_rsi:.1f} → {cur_rsi:.1f}) — entry fires next tick"
    elif cur_rsi > 70:
        signal = "flat"
        distance = 0.0
        desc = (
            f"RSI {cur_rsi:.1f} already above 70 but no fresh cross — "
            f"waiting for next clean cross from below (no position open)"
        )
    else:
        signal = "flat"
        distance = ((70 - cur_rsi) / 70) * 100
        desc = f"RSI {cur_rsi:.1f} needs to cross above 70 (need +{70 - cur_rsi:.1f} points)"

    return StrategyStatus(
        name="rsi_momentum",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=distance,
        details={
            "rsi": cur_rsi,
            "threshold": 70,
            "max_recent_rsi": float(closed["rsi"].tail(30).max()),
        },
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _bb_short_status(repo: Repository | None = None, symbol: str = "SOL", timeframe: str = "1h") -> StrategyStatus:
    df = await fetch_candles(symbol, timeframe, limit=30)
    df = bollinger_bands(df.copy(), 20, 2.0)
    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    price = float(latest["close"])
    high = float(latest["high"])
    upper = float(latest["bb_upper"])
    threshold = upper * 1.02
    has_pos, pos_side = await _has_open_position(repo, "bb_short", symbol)

    if has_pos:
        signal = pos_side
        distance = 0.0
        desc = f"Holding {pos_side} {symbol}. TP limit at entry × 0.98 (no stop loss)."
    elif high > threshold:
        signal = "ready_short"
        distance = 0.0
        desc = f"Bar high ${high:.2f} broke 2% above BB upper ${upper:.2f} (=${threshold:.2f}) — fires next tick"
    else:
        signal = "flat"
        distance = ((threshold - high) / high) * 100
        desc = (
            f"Need bar high ≥ ${threshold:.2f} (BB upper × 1.02). "
            f"Latest bar: high ${high:.2f} ({distance:.2f}% short), close ${price:.2f}."
        )

    return StrategyStatus(
        name="bb_short",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=distance,
        details={
            "bar_high": high,
            "bar_close": price,
            "bb_upper": upper,
            "bb_lower": float(latest["bb_lower"]),
            "trigger_threshold": threshold,
        },
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _sma_rsi_status(repo: Repository | None = None, symbol: str = "ETH", timeframe: str = "1d") -> StrategyStatus:
    df = await fetch_candles(symbol, timeframe, limit=250)
    df = sma(df.copy(), 50, "sma50")
    df = sma(df, 200, "sma200")
    df = rsi(df, 21)
    df["rsi_smooth"] = df["rsi"].rolling(9).mean()

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    price = float(latest["close"])
    sma50 = float(latest["sma50"])
    sma200 = float(latest["sma200"])
    rsi_smooth = float(latest["rsi_smooth"])

    above_fast = price > sma50
    above_slow = price > sma200
    rsi_ok = rsi_smooth > 57
    has_pos, pos_side = await _has_open_position(repo, "sma_rsi", symbol)

    if has_pos:
        signal = pos_side
        distance = 0.0
        desc = f"Holding {pos_side} {symbol}. Will close when price < SMA50 AND RSI < 57"
    elif above_fast and above_slow and rsi_ok:
        signal = "ready_long"
        distance = 0.0
        desc = "All conditions met — READY TO LONG"
    else:
        signal = "flat"
        # Distance = max of the gaps
        gaps = []
        if not above_fast:
            gaps.append(("SMA50", ((sma50 - price) / price) * 100))
        if not above_slow:
            gaps.append(("SMA200", ((sma200 - price) / price) * 100))
        if not rsi_ok:
            gaps.append(("RSI", 57 - rsi_smooth))
        distance = max(g[1] for g in gaps) if gaps else 0.0
        blockers = ", ".join(g[0] for g in gaps)
        desc = f"Blocked by: {blockers}. Price ${price:,.0f}, SMA50 ${sma50:,.0f}, SMA200 ${sma200:,.0f}, RSI {rsi_smooth:.1f}"

    return StrategyStatus(
        name="sma_rsi",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=distance,
        details={
            "sma50": sma50,
            "sma200": sma200,
            "rsi_smooth": rsi_smooth,
            "price_above_sma50": above_fast,
            "price_above_sma200": above_slow,
            "rsi_above_57": rsi_ok,
        },
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _volatility_breakout_status(repo: Repository | None = None, symbol: str = "ETH", timeframe: str = "1h") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=300)
    df = df.copy()

    df["kc_basis"] = pta.ema(df["close"], length=22)
    kc_atr = pta.atr(df["high"], df["low"], df["close"], length=10)
    df["kc_upper"] = df["kc_basis"] + kc_atr * 2.0
    df["kc_lower"] = df["kc_basis"] - kc_atr * 2.0
    df["vol_avg"] = pta.sma(df["volume"], length=18)
    df["rsi"] = pta.rsi(df["close"], length=14)
    df["trend_ema"] = pta.ema(df["close"], length=220)
    adx_df = pta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx_df["ADX_14"] if adx_df is not None and "ADX_14" in adx_df.columns else float("nan")

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]

    closed_price = float(latest["close"])
    kc_upper = float(latest["kc_upper"])
    kc_lower = float(latest["kc_lower"])
    rsi_val = float(latest["rsi"])
    adx_val = float(latest["adx"])
    trend_ema = float(latest["trend_ema"])
    vol = float(latest["volume"])
    vol_avg = float(latest["vol_avg"])

    has_pos, pos_side = await _has_open_position(repo, "volatility_breakout", symbol)

    long_filters = (vol > vol_avg) and (closed_price > trend_ema) and (rsi_val > 50) and (adx_val > 20)
    short_filters = (vol > vol_avg) and (closed_price < trend_ema) and (rsi_val < 50) and (adx_val > 20)
    long_cross = float(prev["close"]) <= float(prev["kc_upper"]) and closed_price > kc_upper
    short_cross = float(prev["close"]) >= float(prev["kc_lower"]) and closed_price < kc_lower

    if has_pos:
        signal = pos_side
        distance = 0.0
        desc = f"Holding {pos_side} {symbol}. ATR×4 stop with breakeven bump (+1.5%) and trail (start +3%, offset 1%)."
    elif long_cross and long_filters:
        signal = "ready_long"
        distance = 0.0
        desc = f"Long breakout fires next tick (close ${closed_price:,.2f} > KC upper ${kc_upper:,.2f}, all filters pass)"
    elif short_cross and short_filters:
        signal = "ready_short"
        distance = 0.0
        desc = f"Short breakdown fires next tick (close ${closed_price:,.2f} < KC lower ${kc_lower:,.2f}, all filters pass)"
    else:
        # Show why we're flat
        upper_dist = ((kc_upper - closed_price) / closed_price) * 100
        lower_dist = ((closed_price - kc_lower) / closed_price) * 100
        nearest = "long" if upper_dist < lower_dist else "short"
        if nearest == "long":
            distance = upper_dist
            blockers = []
            if not (closed_price > trend_ema):
                blockers.append(f"price<EMA220 (${closed_price:,.2f}<${trend_ema:,.2f})")
            if not (rsi_val > 50):
                blockers.append(f"RSI {rsi_val:.0f}≤50")
            if not (adx_val > 20):
                blockers.append(f"ADX {adx_val:.0f}≤20")
            if not (vol > vol_avg):
                blockers.append("volume below avg")
            blocker_txt = f" — blocked by: {', '.join(blockers)}" if blockers else ""
            desc = (
                f"Long needs close>${kc_upper:,.2f} ({distance:.2f}% above current). "
                f"RSI {rsi_val:.0f}, ADX {adx_val:.0f}, vol {vol:,.0f}/{vol_avg:,.0f}{blocker_txt}"
            )
        else:
            distance = lower_dist
            blockers = []
            if not (closed_price < trend_ema):
                blockers.append(f"price>EMA220 (${closed_price:,.2f}>${trend_ema:,.2f})")
            if not (rsi_val < 50):
                blockers.append(f"RSI {rsi_val:.0f}≥50")
            if not (adx_val > 20):
                blockers.append(f"ADX {adx_val:.0f}≤20")
            if not (vol > vol_avg):
                blockers.append("volume below avg")
            blocker_txt = f" — blocked by: {', '.join(blockers)}" if blockers else ""
            desc = (
                f"Short needs close<${kc_lower:,.2f} ({distance:.2f}% below current). "
                f"RSI {rsi_val:.0f}, ADX {adx_val:.0f}, vol {vol:,.0f}/{vol_avg:,.0f}{blocker_txt}"
            )
        signal = "flat"

    return StrategyStatus(
        name="volatility_breakout",
        symbol=symbol,
        timeframe=timeframe,
        price=closed_price,
        signal=signal,
        distance_pct=distance,
        details={
            "close": closed_price,
            "kc_upper": kc_upper,
            "kc_lower": kc_lower,
            "trend_ema_220": trend_ema,
            "rsi_14": rsi_val,
            "adx_14": adx_val,
            "volume": vol,
            "volume_avg_18": vol_avg,
        },
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _btc_mean_reversion_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "15m") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=300)
    df = df.copy()
    df["ema"] = pta.ema(df["close"], length=200)
    df["rsi"] = pta.rsi(df["close"], length=14)
    stoch_df = pta.stoch(df["high"], df["low"], df["close"], k=14, d=3, smooth_k=1)
    k_col = next((c for c in (stoch_df.columns if stoch_df is not None else []) if c.startswith("STOCHk_")), None)
    df["stoch_k"] = stoch_df[k_col] if k_col else float("nan")

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    price = float(latest["close"])
    cur_rsi = float(latest["rsi"])
    cur_k = float(latest["stoch_k"])
    cur_ema = float(latest["ema"])

    has_pos, pos_side = await _has_open_position(repo, "btc_mean_reversion", symbol)

    long_conds = (cur_rsi < 20, cur_k < 25, price > cur_ema * 0.9)
    short_conds = (cur_rsi > 65, cur_k > 75, price < cur_ema)

    if has_pos:
        signal = pos_side
        distance = 0.0
        desc = f"Holding {pos_side} {symbol}. Fixed exits: SL {'-' if pos_side == 'long' else '+'}4%, TP {'+' if pos_side == 'long' else '-'}6% from entry."
    elif all(long_conds):
        signal = "ready_long"
        distance = 0.0
        desc = (
            f"All long conditions met: RSI {cur_rsi:.1f}<20, %K {cur_k:.1f}<25, "
            f"close ${price:,.0f} > EMA200×0.9 (${cur_ema * 0.9:,.0f}) — fires next tick"
        )
    elif all(short_conds):
        signal = "ready_short"
        distance = 0.0
        desc = (
            f"All short conditions met: RSI {cur_rsi:.1f}>65, %K {cur_k:.1f}>75, "
            f"close ${price:,.0f} < EMA200 (${cur_ema:,.0f}) — fires next tick"
        )
    else:
        # Pick the closer side and explain what's blocking
        long_score = sum(long_conds)
        short_score = sum(short_conds)
        if long_score >= short_score:
            blockers = []
            if not long_conds[0]:
                blockers.append(f"RSI {cur_rsi:.1f}≥20")
            if not long_conds[1]:
                blockers.append(f"%K {cur_k:.1f}≥25")
            if not long_conds[2]:
                blockers.append(f"price<EMA200×0.9 (${price:,.0f}<${cur_ema * 0.9:,.0f})")
            distance = max(0.0, cur_rsi - 20)
            desc = (
                f"Long needs RSI<20+%K<25+price>EMA200×0.9. "
                f"RSI {cur_rsi:.1f}, %K {cur_k:.1f}, EMA200 ${cur_ema:,.0f}. "
                f"Blocked by: {', '.join(blockers)}"
            )
        else:
            blockers = []
            if not short_conds[0]:
                blockers.append(f"RSI {cur_rsi:.1f}≤65")
            if not short_conds[1]:
                blockers.append(f"%K {cur_k:.1f}≤75")
            if not short_conds[2]:
                blockers.append(f"price>EMA200 (${price:,.0f}>${cur_ema:,.0f})")
            distance = max(0.0, 65 - cur_rsi)
            desc = (
                f"Short needs RSI>65+%K>75+price<EMA200. "
                f"RSI {cur_rsi:.1f}, %K {cur_k:.1f}, EMA200 ${cur_ema:,.0f}. "
                f"Blocked by: {', '.join(blockers)}"
            )
        signal = "flat"

    return StrategyStatus(
        name="btc_mean_reversion",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=distance,
        details={
            "close": price,
            "rsi_14": cur_rsi,
            "stoch_k_14": cur_k,
            "ema_200": cur_ema,
        },
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _hash_momentum_status(repo: Repository | None = None, symbol: str = "SOL", timeframe: str = "4h") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=100)
    df = df.copy()
    df["mom0"] = df["close"] - df["close"].shift(13)
    df["mom_stdev"] = df["mom0"].rolling(39).std()
    df["mom_norm"] = df.apply(
        lambda r: r["mom0"] / r["mom_stdev"] if r["mom_stdev"] > 0 else 0.0, axis=1
    )
    df["atr"] = pta.atr(df["high"], df["low"], df["close"], length=14)
    df["ema28"] = pta.ema(df["close"], length=28)

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    price = float(latest["close"])
    mom0 = float(latest["mom0"]) if not pd.isna(latest["mom0"]) else 0.0
    mom_norm = float(latest["mom_norm"]) if not pd.isna(latest["mom_norm"]) else 0.0
    atr = float(latest["atr"]) if not pd.isna(latest["atr"]) else 0.0
    ema28 = float(latest["ema28"]) if not pd.isna(latest["ema28"]) else price
    threshold = atr * 2.25
    has_pos, pos_side = await _has_open_position(repo, "hash_momentum", symbol)

    if has_pos:
        signal = pos_side
        desc = f"Holding {pos_side}. Fixed SL 2.2%, TP at 2.5× risk."
    elif mom0 > threshold and mom_norm > 0.5 and price > ema28:
        signal = "ready_long"
        desc = f"Long momentum: mom={mom0:.2f}>{threshold:.2f}, norm={mom_norm:.2f}, price>${ema28:,.2f} EMA28"
    elif mom0 < -threshold and mom_norm < -0.5 and price < ema28:
        signal = "ready_short"
        desc = f"Short momentum: mom={mom0:.2f}<{-threshold:.2f}, norm={mom_norm:.2f}, price<${ema28:,.2f} EMA28"
    else:
        signal = "flat"
        desc = (
            f"Flat. mom={mom0:.2f} (need >{threshold:.2f}), norm={mom_norm:.2f} (need >0.5), "
            f"EMA28=${ema28:,.2f}, price=${price:,.2f}"
        )

    return StrategyStatus(
        name="hash_momentum",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=max(0.0, (threshold - abs(mom0)) / price * 100) if abs(mom0) < threshold else 0.0,
        details={"close": price, "mom0": mom0, "mom_norm": mom_norm, "threshold": threshold, "ema28": ema28},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _ema_crossover_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "1h") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=50)
    df = df.copy()
    df["ema7"] = pta.ema(df["close"], length=7)
    df["ema19"] = pta.ema(df["close"], length=19)

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]
    price = float(latest["close"])
    cur_fast = float(latest["ema7"])
    cur_slow = float(latest["ema19"])
    prev_fast = float(prev["ema7"]) if not pd.isna(prev["ema7"]) else cur_fast
    prev_slow = float(prev["ema19"]) if not pd.isna(prev["ema19"]) else cur_slow
    has_pos, pos_side = await _has_open_position(repo, "ema_crossover", symbol)

    bullish = cur_fast > cur_slow
    bull_cross = prev_fast <= prev_slow and cur_fast > cur_slow
    bear_cross = prev_fast >= prev_slow and cur_fast < cur_slow

    if has_pos:
        signal = pos_side
        desc = f"Holding {pos_side}. EMA7={cur_fast:,.2f}, EMA19={cur_slow:,.2f}. Exit on opposite cross or SL."
    elif bull_cross:
        signal = "ready_long"
        desc = f"Bullish cross just fired: EMA7={cur_fast:,.2f} > EMA19={cur_slow:,.2f}"
    elif bear_cross:
        signal = "ready_short"
        desc = f"Bearish cross just fired: EMA7={cur_fast:,.2f} < EMA19={cur_slow:,.2f}"
    else:
        signal = "flat"
        gap = abs(cur_fast - cur_slow)
        desc = (
            f"Trend {'bullish' if bullish else 'bearish'}: EMA7={cur_fast:,.2f}, EMA19={cur_slow:,.2f}, "
            f"gap={gap:,.2f}. Waiting for crossover."
        )

    return StrategyStatus(
        name="ema_crossover",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=abs(cur_fast - cur_slow) / price * 100,
        details={"close": price, "ema7": cur_fast, "ema19": cur_slow, "bullish": bullish},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _cdc_macd_status(repo: Repository | None = None, symbol: str = "SOL", timeframe: str = "1d") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=60)
    df = df.copy()
    df["ema12"] = pta.ema(df["close"], length=12)
    df["ema26"] = pta.ema(df["close"], length=26)

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]
    price = float(latest["close"])
    cur_fast = float(latest["ema12"]) if not pd.isna(latest["ema12"]) else price
    cur_slow = float(latest["ema26"]) if not pd.isna(latest["ema26"]) else price
    prev_fast = float(prev["ema12"]) if not pd.isna(prev["ema12"]) else cur_fast
    prev_slow = float(prev["ema26"]) if not pd.isna(prev["ema26"]) else cur_slow
    has_pos, pos_side = await _has_open_position(repo, "cdc_macd", symbol)

    buy = prev_fast <= prev_slow and cur_fast > cur_slow
    sell = prev_fast >= prev_slow and cur_fast < cur_slow

    if has_pos:
        signal = pos_side
        desc = f"Holding long. EMA12={cur_fast:,.2f} vs EMA26={cur_slow:,.2f}. Exit on bearish cross."
    elif buy:
        signal = "ready_long"
        desc = f"Bullish cross: EMA12={cur_fast:,.2f} > EMA26={cur_slow:,.2f}"
    else:
        signal = "flat"
        bullish = cur_fast > cur_slow
        desc = (
            f"{'Bullish' if bullish else 'Bearish'}: EMA12={cur_fast:,.2f}, EMA26={cur_slow:,.2f}. "
            f"Long-only — waiting for EMA12 to cross above EMA26."
        )

    return StrategyStatus(
        name="cdc_macd",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=abs(cur_fast - cur_slow) / price * 100,
        details={"close": price, "ema12": cur_fast, "ema26": cur_slow, "above": cur_fast > cur_slow},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _keltner_breakout_status(repo: Repository | None = None, symbol: str = "ETH", timeframe: str = "4h") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=250)
    df = df.copy()
    df["ema200"] = pta.ema(df["close"], length=200)
    atr = pta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr"] = atr
    df["kc_mid"] = pta.ema(df["close"], length=20)
    df["kc_upper"] = df["kc_mid"] + atr * 2.0
    df["kc_lower"] = df["kc_mid"] - atr * 2.0

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    price = float(latest["close"])
    ema200 = float(latest["ema200"]) if not pd.isna(latest["ema200"]) else price
    kc_upper = float(latest["kc_upper"]) if not pd.isna(latest["kc_upper"]) else price * 1.05
    kc_lower = float(latest["kc_lower"]) if not pd.isna(latest["kc_lower"]) else price * 0.95
    has_pos, pos_side = await _has_open_position(repo, "keltner_breakout", symbol)

    trend_up = price > ema200
    breakout = price > kc_upper
    dist_to_upper = ((kc_upper - price) / price) * 100

    if has_pos:
        signal = pos_side
        desc = f"Holding long. ATR×4 SL, 20% TP, KC-lower exit. EMA200=${ema200:,.2f}."
    elif trend_up and breakout:
        signal = "ready_long"
        desc = f"Breakout! close ${price:,.2f} > KC upper ${kc_upper:,.2f} AND above EMA200 ${ema200:,.2f}"
    else:
        signal = "flat"
        blockers = []
        if not trend_up:
            blockers.append(f"below EMA200 (${price:,.2f}<${ema200:,.2f})")
        if not breakout:
            blockers.append(f"below KC upper (${price:,.2f}<${kc_upper:,.2f}, {dist_to_upper:.2f}% away)")
        desc = f"Flat. Blocked by: {', '.join(blockers)}"

    return StrategyStatus(
        name="keltner_breakout",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=max(0.0, dist_to_upper),
        details={"close": price, "ema200": ema200, "kc_upper": kc_upper, "kc_lower": kc_lower, "trend_up": trend_up},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _pivot_supertrend_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "4h") -> StrategyStatus:
    import pandas_ta as pta
    from hypertrade.strategies.pivot_supertrend import _compute_pivot_supertrend

    df = await fetch_candles(symbol, timeframe, limit=300)
    df = df.copy()
    df["ema200"] = pta.ema(df["close"], length=200)
    df = _compute_pivot_supertrend(df)

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]
    price = float(latest["close"])
    ema200 = float(latest["ema200"]) if not pd.isna(latest["ema200"]) else price
    trend = int(latest["ps_trend"]) if not pd.isna(latest["ps_trend"]) else 1
    prev_trend = int(prev["ps_trend"]) if not pd.isna(prev["ps_trend"]) else 1
    has_pos, pos_side = await _has_open_position(repo, "pivot_supertrend", symbol)

    bsignal = trend == 1 and prev_trend == -1
    ssignal = trend == -1 and prev_trend == 1

    if has_pos:
        signal = pos_side
        desc = f"Holding {pos_side}. PS trend={trend}. 1% SL. EMA200=${ema200:,.2f}."
    elif bsignal and price > ema200:
        signal = "ready_long"
        desc = f"PS flipped bullish: trend {prev_trend}→{trend}, price ${price:,.2f} > EMA200 ${ema200:,.2f}"
    elif ssignal and price < ema200:
        signal = "ready_short"
        desc = f"PS flipped bearish: trend {prev_trend}→{trend}, price ${price:,.2f} < EMA200 ${ema200:,.2f}"
    else:
        trend_word = "bullish" if trend == 1 else "bearish"
        desc = f"PS trend {trend_word} (trend={trend}), price ${price:,.2f}, EMA200 ${ema200:,.2f}. Waiting for flip."
        signal = "flat"

    return StrategyStatus(
        name="pivot_supertrend",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=0.0,
        details={"close": price, "ps_trend": trend, "ema200": ema200},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _macd_zero_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "1d") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=60)
    df = df.copy()
    macd_df = pta.macd(df["close"], fast=12, slow=26, signal=9)
    macd_col = next((c for c in (macd_df.columns if macd_df is not None else []) if "MACD_" in c and "MACDs" not in c and "MACDh" not in c), None)
    if macd_col and macd_df is not None:
        df["macd"] = macd_df[macd_col]
    else:
        df["macd"] = float("nan")

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]
    price = float(latest["close"])
    cur_macd = float(latest["macd"]) if not pd.isna(latest["macd"]) else 0.0
    prev_macd = float(prev["macd"]) if not pd.isna(prev["macd"]) else 0.0
    has_pos, pos_side = await _has_open_position(repo, "macd_zero", symbol)

    cross_up = prev_macd <= 0 and cur_macd > 0
    cross_down = prev_macd >= 0 and cur_macd < 0

    if has_pos:
        signal = pos_side
        desc = f"Holding long. MACD={cur_macd:.2f}. Exit when MACD crosses below 0."
    elif cross_up:
        signal = "ready_long"
        desc = f"MACD just crossed above 0: {prev_macd:.2f} → {cur_macd:.2f}"
    else:
        signal = "flat"
        if cur_macd > 0:
            desc = f"MACD {cur_macd:.2f} above 0 but no fresh cross. Waiting for next clean cross from below."
        else:
            desc = f"MACD {cur_macd:.2f} below 0 (need to cross above). Distance: {abs(cur_macd):.2f} points."

    return StrategyStatus(
        name="macd_zero",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=abs(cur_macd) / price * 100 if cur_macd < 0 else 0.0,
        details={"close": price, "macd": cur_macd, "prev_macd": prev_macd},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _moon_phases_status(repo: Repository | None = None, symbol: str = "BTC", timeframe: str = "1d") -> StrategyStatus:
    from hypertrade.strategies.moon_phases import _lunar_day, _LUNAR_CYCLE

    df = await fetch_candles(symbol, timeframe, limit=35)
    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]

    price = float(latest["close"])
    ts_latest = float(latest["timestamp"].timestamp() * 1000) if hasattr(latest["timestamp"], "timestamp") else float(latest["timestamp"])
    ts_prev = float(prev["timestamp"].timestamp() * 1000) if hasattr(prev["timestamp"], "timestamp") else float(prev["timestamp"])

    ld_cur = _lunar_day(ts_latest)
    ld_prev = _lunar_day(ts_prev)
    is_full = 13 <= ld_cur <= 15
    is_new = ld_cur in (0, 1)
    full_start = is_full and not (13 <= ld_prev <= 15)

    has_pos, pos_side = await _has_open_position(repo, "moon_phases", symbol)

    if has_pos:
        signal = pos_side
        desc = f"Holding long. Lunar day {ld_cur}/29.5. Exit at new moon (day 0-1) or SL 5% / TP 10%."
    elif full_start:
        signal = "ready_long"
        desc = f"Full moon started (lunar day {ld_cur})! Entry fires next tick."
    elif is_full:
        signal = "flat"
        desc = f"Full moon active (lunar day {ld_cur}) but no fresh transition. Waiting for next full moon start."
    else:
        # Days to next full moon
        if ld_cur < 13:
            days_to_full = 13 - ld_cur
        elif ld_cur > 15:
            days_to_full = int(_LUNAR_CYCLE) - ld_cur + 13
        else:
            days_to_full = 0
        signal = "flat"
        desc = f"Lunar day {ld_cur}/29.5. {'New moon' if is_new else 'Waning/Waxing'}. ~{days_to_full} days to next full moon."

    return StrategyStatus(
        name="moon_phases",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=0.0,
        details={"close": price, "lunar_day": ld_cur, "is_full_moon": is_full, "is_new_moon": is_new},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


async def _penguin_volatility_status(repo: Repository | None = None, symbol: str = "ETH", timeframe: str = "1h") -> StrategyStatus:
    import pandas_ta as pta

    df = await fetch_candles(symbol, timeframe, limit=100)
    df = df.copy()
    basis = pta.sma(df["close"], length=20)
    stdev_s = df["close"].rolling(20).std()
    atr = pta.atr(df["high"], df["low"], df["close"], length=20)
    upper_bb = basis + 2.0 * stdev_s
    upper_kc = basis + 2.0 * atr
    df["diff"] = (upper_bb - upper_kc) / upper_kc * 100
    df["rsi_diff"] = pta.rsi(df["diff"], length=14)
    df["rsi_diff2"] = pta.sma(df["rsi_diff"], length=7)
    df["fast_ma"] = pta.ema(df["close"], length=12)
    df["slow_ma"] = pta.ema(df["close"], length=26)
    df["ohlc4"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    df["apcdc"] = pta.ema(df["ohlc4"], length=2)

    closed = df.iloc[:-1]
    latest = closed.iloc[-1]
    prev = closed.iloc[-2]

    price = float(latest["close"])
    fast_ma = float(latest["fast_ma"]) if not pd.isna(latest["fast_ma"]) else price
    slow_ma = float(latest["slow_ma"]) if not pd.isna(latest["slow_ma"]) else price
    apcdc = float(latest["apcdc"]) if not pd.isna(latest["apcdc"]) else price
    cur_rd = float(latest["rsi_diff"]) if not pd.isna(latest["rsi_diff"]) else 50.0
    cur_rd2 = float(latest["rsi_diff2"]) if not pd.isna(latest["rsi_diff2"]) else 50.0
    prev_rd = float(prev["rsi_diff"]) if not pd.isna(prev["rsi_diff"]) else 50.0
    prev_rd2 = float(prev["rsi_diff2"]) if not pd.isna(prev["rsi_diff2"]) else 50.0

    is_green = fast_ma > slow_ma and apcdc > fast_ma
    is_yellow = fast_ma > slow_ma and apcdc < fast_ma
    can_long = is_green or is_yellow
    entry_timing = prev_rd2 >= prev_rd and cur_rd2 < cur_rd and can_long
    state = "Green" if is_green else "Yellow" if is_yellow else ("Red" if fast_ma < slow_ma and apcdc < fast_ma else "Blue")

    has_pos, pos_side = await _has_open_position(repo, "penguin_volatility", symbol)

    if has_pos:
        signal = pos_side
        desc = f"Holding long. State={state}, rsi_diff={cur_rd:.1f} vs avg={cur_rd2:.1f}. Exit when rsi_diff crosses below avg."
    elif entry_timing:
        signal = "ready_long"
        desc = f"Entry: rsi_diff {cur_rd:.1f} > rsi_diff2 {cur_rd2:.1f} (just crossed), state={state}"
    else:
        signal = "flat"
        desc = (
            f"State={state} ({'can trade' if can_long else 'no trade — bearish state'}). "
            f"rsi_diff={cur_rd:.1f}, avg={cur_rd2:.1f}. "
            f"EMA12={fast_ma:,.2f}, EMA26={slow_ma:,.2f}."
        )

    return StrategyStatus(
        name="penguin_volatility",
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        signal=signal,
        distance_pct=0.0,
        details={"close": price, "state": state, "fast_ma": fast_ma, "slow_ma": slow_ma, "rsi_diff": cur_rd, "rsi_diff2": cur_rd2},
        description=desc,
        has_open_position=has_pos,
        position_side=pos_side,
    )


_STATUS_FUNCTIONS: dict = {
    "supertrend": _supertrend_status,
    "rsi_momentum": _rsi_momentum_status,
    "bb_short": _bb_short_status,
    "sma_rsi": _sma_rsi_status,
    "volatility_breakout": _volatility_breakout_status,
    "btc_mean_reversion": _btc_mean_reversion_status,
    "hash_momentum": _hash_momentum_status,
    "ema_crossover": _ema_crossover_status,
    "cdc_macd": _cdc_macd_status,
    "keltner_breakout": _keltner_breakout_status,
    "pivot_supertrend": _pivot_supertrend_status,
    "macd_zero": _macd_zero_status,
    "moon_phases": _moon_phases_status,
    "penguin_volatility": _penguin_volatility_status,
}


async def get_all_status(repo: Repository | None = None, strategies: list | None = None) -> list[StrategyStatus]:
    import asyncio
    import logging
    _log = logging.getLogger(__name__)

    if strategies:
        # Build coroutines using symbol/timeframe from the live strategy objects.
        # Strategies without a matching status function are silently skipped.
        coros: list = []
        names: list[str] = []
        for s in strategies:
            fn = _STATUS_FUNCTIONS.get(s.name)
            if fn is not None:
                coros.append(fn(repo, s.symbol, s.timeframe))
                names.append(s.name)
        if coros:
            results = await asyncio.gather(*coros, return_exceptions=True)
            out = []
            for name, result in zip(names, results):
                if isinstance(result, Exception):
                    _log.warning("indicator status failed for %s: %s", name, result)
                else:
                    out.append(result)
            return out

    # Fallback: run all known status functions with their hardcoded defaults.
    fallback_names = list(_STATUS_FUNCTIONS.keys())
    fallback_coros = [fn(repo) for fn in _STATUS_FUNCTIONS.values()]
    results = await asyncio.gather(*fallback_coros, return_exceptions=True)
    out = []
    for name, result in zip(fallback_names, results):
        if isinstance(result, Exception):
            _log.warning("indicator status failed for %s: %s", name, result)
        else:
            out.append(result)
    return out
