"""Technical indicator wrappers using pandas-ta."""

import pandas as pd
import pandas_ta as ta


def supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 8.5
) -> pd.DataFrame:
    """Calculate SuperTrend indicator.

    Returns DataFrame with 'supertrend', 'supertrend_direction' columns.
    Direction: 1 = bullish, -1 = bearish.
    """
    st = ta.supertrend(df["high"], df["low"], df["close"], length=period, multiplier=multiplier)
    if st is None:
        return df

    # pandas-ta returns columns like SUPERT_10_8.5, SUPERTd_10_8.5, etc.
    st_cols = st.columns.tolist()
    trend_col = [c for c in st_cols if c.startswith("SUPERT_")][0]
    dir_col = [c for c in st_cols if c.startswith("SUPERTd_")][0]

    df["supertrend"] = st[trend_col]
    df["supertrend_direction"] = st[dir_col]
    return df


def rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate RSI."""
    df["rsi"] = ta.rsi(df["close"], period)
    return df


def sma(df: pd.DataFrame, period: int = 50, col_name: str | None = None) -> pd.DataFrame:
    """Calculate Simple Moving Average."""
    name = col_name or f"sma_{period}"
    df[name] = ta.sma(df["close"], period)
    return df


def ema(df: pd.DataFrame, period: int = 20, col_name: str | None = None) -> pd.DataFrame:
    """Calculate Exponential Moving Average."""
    name = col_name or f"ema_{period}"
    df[name] = ta.ema(df["close"], period)
    return df


def bollinger_bands(
    df: pd.DataFrame, period: int = 20, std: float = 2.0
) -> pd.DataFrame:
    """Calculate Bollinger Bands."""
    bb = ta.bbands(df["close"], period, std)
    if bb is None:
        return df

    cols = bb.columns.tolist()
    df["bb_lower"] = bb[[c for c in cols if c.startswith("BBL_")][0]]
    df["bb_mid"] = bb[[c for c in cols if c.startswith("BBM_")][0]]
    df["bb_upper"] = bb[[c for c in cols if c.startswith("BBU_")][0]]
    return df


def atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Calculate Average True Range."""
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], period)
    return df


def stochastic(
    df: pd.DataFrame, k_period: int = 14, d_period: int = 3, smooth_k: int = 3
) -> pd.DataFrame:
    """Calculate Stochastic Oscillator (%K and %D)."""
    s = ta.stoch(df["high"], df["low"], df["close"], k=k_period, d=d_period, smooth_k=smooth_k)
    if s is None:
        return df
    cols = s.columns.tolist()
    df["stoch_k"] = s[[c for c in cols if c.startswith("STOCHk_")][0]]
    df["stoch_d"] = s[[c for c in cols if c.startswith("STOCHd_")][0]]
    return df


def macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Calculate MACD."""
    m = ta.macd(df["close"], fast, slow, signal)
    if m is None:
        return df

    cols = m.columns.tolist()
    df["macd"] = m[[c for c in cols if c.startswith("MACD_")][0]]
    df["macd_signal"] = m[[c for c in cols if c.startswith("MACDs_")][0]]
    df["macd_hist"] = m[[c for c in cols if c.startswith("MACDh_")][0]]
    return df
