"""Binance public data dump loader.

Pulls historical OHLCV from https://data.binance.vision (free, no API key).
Cached as parquet under bot/data/historical/. Returns DataFrames in the
same shape as data.feed.fetch_candles so backtest code can swap sources.

Available data: BTCUSDT/ETHUSDT from 2017, most majors from 2019-2020.
HYPE is NOT on Binance — for HYPE use HL's own feed (limited history).

Usage:
    df = await load_dump("BTC", "1d", days=2000)
    df = await load_dump("ETH", "4h", start="2020-01-01", end="2024-12-31")
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "historical"
BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"
# Binance kline columns
COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_buy_base",
    "taker_buy_quote", "ignore",
]

# Map our short symbols to Binance trading pairs
SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "XRP": "XRPUSDT",
    "AVAX": "AVAXUSDT",
    "SUI": "SUIUSDT",
    "LINK": "LINKUSDT",
    "ADA": "ADAUSDT",
    "BNB": "BNBUSDT",
    "MATIC": "MATICUSDT",
    "DOT": "DOTUSDT",
}

# Map our timeframe strings to Binance interval strings
TF_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w",
}


def _cache_path(pair: str, interval: str, year: int, month: int) -> Path:
    return CACHE_DIR / pair / interval / f"{pair}-{interval}-{year:04d}-{month:02d}.csv.gz"


async def _fetch_month(
    session: aiohttp.ClientSession, pair: str, interval: str,
    year: int, month: int,
) -> pd.DataFrame | None:
    """Fetch one month of kline data. Returns None if not available
    (e.g. before listing date)."""
    cache = _cache_path(pair, interval, year, month)
    if cache.exists():
        try:
            df = pd.read_csv(cache, compression="gzip", parse_dates=["timestamp"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            return df
        except Exception:
            cache.unlink(missing_ok=True)

    fname = f"{pair}-{interval}-{year:04d}-{month:02d}.zip"
    url = f"{BASE_URL}/{pair}/{interval}/{fname}"
    try:
        async with session.get(url, timeout=60) as resp:
            if resp.status == 404:
                return None  # month before listing or after current
            resp.raise_for_status()
            data = await resp.read()
    except Exception as e:
        logger.warning("Binance dump fetch failed for %s: %s", url, e)
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            csv_name = fname.replace(".zip", ".csv")
            with zf.open(csv_name) as f:
                df = pd.read_csv(f, header=None, names=COLS)
    except Exception as e:
        logger.warning("Binance dump parse failed for %s: %s", fname, e)
        return None

    # Newer dumps have a "open_time" string header row — drop if present
    if df.iloc[0]["open_time"] == "open_time":
        df = df.iloc[1:].reset_index(drop=True)

    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["open_time"] = pd.to_numeric(df["open_time"])
    # Binance timestamps are ms (13 digits, ~1e12) until ~2025, then
    # microseconds (16 digits, ~1e15). 1e14 cleanly separates them.
    if df["open_time"].iloc[0] > 1e14:
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="us", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]

    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, compression="gzip", index=False)
    return df


async def load_dump(
    symbol: str, timeframe: str, days: int | None = None,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
) -> pd.DataFrame:
    """Load Binance dump data, returning DataFrame with columns:
    timestamp, open, high, low, close, volume.

    Specify either days (relative to now) or start+end dates.
    """
    pair = SYMBOL_MAP.get(symbol.upper())
    if pair is None:
        raise ValueError(f"No Binance mapping for {symbol!r}; add to SYMBOL_MAP")
    interval = TF_MAP.get(timeframe)
    if interval is None:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")

    now = datetime.now(timezone.utc)
    if start is None and end is None:
        if days is None:
            raise ValueError("Need either days or start+end")
        end_dt = now
        start_dt = now - timedelta(days=days)
    else:
        end_dt = pd.to_datetime(end or now, utc=True).to_pydatetime()
        start_dt = pd.to_datetime(start, utc=True).to_pydatetime()

    # Build month list
    months: list[tuple[int, int]] = []
    cur = datetime(start_dt.year, start_dt.month, 1, tzinfo=timezone.utc)
    end_month = datetime(end_dt.year, end_dt.month, 1, tzinfo=timezone.utc)
    while cur <= end_month:
        months.append((cur.year, cur.month))
        # Step to next month
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            cur = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)

    async with aiohttp.ClientSession() as session:
        chunks = await asyncio.gather(*[
            _fetch_month(session, pair, interval, y, m) for y, m in months
        ])

    valid = [c for c in chunks if c is not None and not c.empty]
    if not valid:
        raise RuntimeError(
            f"No Binance data found for {pair} {interval} between "
            f"{start_dt.date()} and {end_dt.date()}"
        )

    df = pd.concat(valid, ignore_index=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Trim to exact window
    df = df[(df["timestamp"] >= pd.Timestamp(start_dt)) & (df["timestamp"] <= pd.Timestamp(end_dt))]
    df = df.reset_index(drop=True)

    return df
