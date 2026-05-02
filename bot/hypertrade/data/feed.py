"""OHLCV data feed from HyperLiquid REST + WebSocket API."""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

import aiohttp
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
import pandas as pd

logger = logging.getLogger(__name__)

HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


async def fetch_candles(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 300,
) -> pd.DataFrame:
    """Fetch OHLCV candles from HyperLiquid REST API."""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    tf_ms = TIMEFRAME_SECONDS.get(timeframe, 3600) * 1000
    start_time = now - (limit * tf_ms)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": timeframe,
            "startTime": start_time,
            "endTime": now,
        },
    }

    data = None
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)),
            reraise=True,
        ):
            with attempt:
                if attempt.retry_state.attempt_number > 1:
                    logger.info(
                        "Candle fetch retry %d/3 for %s %s",
                        attempt.retry_state.attempt_number, symbol, timeframe,
                    )
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        HYPERLIQUID_INFO_URL,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        # Retry on 5xx; treat 4xx as terminal.
                        if 500 <= resp.status < 600:
                            raise aiohttp.ClientResponseError(
                                resp.request_info, resp.history,
                                status=resp.status, message=f"HL {resp.status}",
                            )
                        if resp.status != 200:
                            logger.error(
                                "Failed to fetch candles: %s %s",
                                resp.status, await resp.text(),
                            )
                            return pd.DataFrame()
                        data = await resp.json()
    except RetryError as e:
        logger.error("Candle fetch gave up after retries for %s %s: %s",
                     symbol, timeframe, e)
        return pd.DataFrame()
    except aiohttp.ClientError as e:
        logger.error("Candle fetch network error for %s %s: %s",
                     symbol, timeframe, e)
        return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    rows = [
        {
            "timestamp": datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc),
            "open": float(c["o"]),
            "high": float(c["h"]),
            "low": float(c["l"]),
            "close": float(c["c"]),
            "volume": float(c["v"]),
        }
        for c in data
    ]

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# --- WebSocket real-time feed ---

PriceCallback = Callable[[str, float], None]
CandleCallback = Callable[[str, str, dict], None]


class HyperLiquidWebSocket:
    """Real-time data feed via HyperLiquid WebSocket.

    Subscribes to:
    - allMids: real-time mid prices for all coins
    - candle: per-symbol candle updates
    """

    def __init__(self) -> None:
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._price_callbacks: list[PriceCallback] = []
        self._candle_callbacks: list[CandleCallback] = []
        self._subscriptions: list[dict] = []
        self._latest_prices: dict[str, float] = {}
        self._reconnect_delay = 1.0

    def on_price(self, callback: PriceCallback) -> None:
        """Register callback for price updates: callback(symbol, price)"""
        self._price_callbacks.append(callback)

    def on_candle(self, callback: CandleCallback) -> None:
        """Register callback for candle updates: callback(symbol, timeframe, candle_dict)"""
        self._candle_callbacks.append(callback)

    def subscribe_candles(self, symbol: str, timeframe: str) -> None:
        """Subscribe to candle updates for a symbol/timeframe pair."""
        self._subscriptions.append({
            "type": "subscribe",
            "subscription": {"type": "candle", "coin": symbol, "interval": timeframe},
        })

    def get_price(self, symbol: str) -> float:
        """Get latest cached price for a symbol."""
        return self._latest_prices.get(symbol, 0.0)

    async def connect(self) -> None:
        """Connect and start receiving data."""
        self._running = True
        while self._running:
            try:
                await self._run()
            except Exception:
                if not self._running:
                    break
                logger.exception(
                    "WebSocket disconnected, reconnecting in %.0fs",
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def _run(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(HYPERLIQUID_WS_URL)
            logger.info("WebSocket connected to %s", HYPERLIQUID_WS_URL)
            self._reconnect_delay = 1.0

            # Subscribe to allMids for real-time prices
            await self._ws.send_json({
                "method": "subscribe",
                "subscription": {"type": "allMids"},
            })

            # Subscribe to candle channels
            for sub in self._subscriptions:
                await self._ws.send_json(sub)
                logger.info(
                    "Subscribed to candles: %s %s",
                    sub["subscription"]["coin"],
                    sub["subscription"]["interval"],
                )

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        channel = data.get("channel")

        if channel == "allMids":
            mids = data.get("data", {}).get("mids", {})
            for symbol, price_str in mids.items():
                price = float(price_str)
                self._latest_prices[symbol] = price
                for cb in self._price_callbacks:
                    try:
                        cb(symbol, price)
                    except Exception:
                        logger.exception("Price callback error")

        elif channel == "candle":
            candle_data = data.get("data", {})
            symbol = candle_data.get("s", "")
            # Symbol comes as "BTC" from the subscription
            # Candle fields: t (time), T (close time), s (symbol),
            # i (interval), o, h, l, c, v, n
            interval = candle_data.get("i", "")
            for cb in self._candle_callbacks:
                try:
                    cb(symbol, interval, candle_data)
                except Exception:
                    logger.exception("Candle callback error")

    async def close(self) -> None:
        """Disconnect WebSocket."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("WebSocket disconnected")
