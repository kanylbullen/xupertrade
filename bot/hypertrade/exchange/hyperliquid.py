"""Live HyperLiquid exchange implementation.

Uses hyperliquid-python-sdk for real order execution.
Requires HYPERLIQUID_PRIVATE_KEY in .env.

For safety, run with HYPERLIQUID_TESTNET=true first to verify everything
end-to-end against testnet (free testnet USDC from the faucet) before
moving to mainnet.
"""

import asyncio
import logging
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from eth_account import Account
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.error import ServerError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hypertrade.config import settings
from hypertrade.exchange.base import (
    Balance,
    Exchange,
    Order,
    OrderStatus,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)

# Errors that are worth retrying — transient network or HL infra issues.
# 4xx ClientError (e.g. validation, insufficient margin) is NOT in this list:
# retrying would just re-fail the same way.
_RETRYABLE_ERRORS = (
    aiohttp.ClientError,
    socket.gaierror,
    ServerError,
    ConnectionError,
    TimeoutError,
)


def _is_retryable_server_error(exc: BaseException) -> bool:
    """ServerError covers all 4xx and 5xx — only retry 5xx + 408/429."""
    if isinstance(exc, ServerError):
        msg = str(exc)
        return any(code in msg for code in ("502", "503", "504", "408", "429"))
    return isinstance(exc, _RETRYABLE_ERRORS)


class HyperLiquidExchange(Exchange):
    def __init__(self) -> None:
        if not settings.hyperliquid_private_key:
            raise ValueError(
                "HYPERLIQUID_PRIVATE_KEY missing in env — required for live mode."
            )

        base_url = (
            constants.TESTNET_API_URL
            if settings.is_testnet
            else constants.MAINNET_API_URL
        )
        self._account = Account.from_key(settings.hyperliquid_private_key)
        # API wallet pattern: signing wallet differs from trading account.
        # If hyperliquid_account_address is set, orders are submitted on
        # behalf of that address (the "main" account). If empty, the
        # signing wallet IS the trading account.
        self._account_address = (
            settings.hyperliquid_account_address.strip()
            or self._account.address
        )
        # Pass `timeout` into the SDK so the underlying requests.Session
        # actually kills hung TCP connections at the socket level. Without
        # this, asyncio.wait_for would raise TimeoutError but leave the
        # executor thread stuck on a hanging requests.post — eventually
        # exhausting the pool (audit M2 / PR #20 review). The SDK uses
        # the order-timeout deadline since the same Info/Exchange object
        # serves both reads and writes; reads complete much faster than
        # the order-timeout window in normal conditions, and our Python-
        # level wait_for still applies the tighter read-timeout on top.
        sdk_timeout = settings.hl_order_timeout_seconds
        # Construct Info + Exchange with retry. The SDK's HLExchange
        # constructor internally creates an Info() with `meta`/`spot_meta`
        # positional args; if either is None it triggers a network fetch
        # right there. Without the retry, a transient HL outage during
        # bot start = container exit + restart-loop until HL recovers
        # (witnessed 2026-05-09 — bot was in restart-loop for 4.5h while
        # HL testnet was down, despite the meta() try/except on line 109
        # which only protected our own meta call, not the SDK's internal
        # one). Retry buys us through brief glitches.
        last_exc: Exception | None = None
        for attempt in range(1, settings.hl_init_retry_attempts + 1):
            try:
                self._info = Info(
                    base_url, skip_ws=True, timeout=sdk_timeout,
                )
                self._exchange = HLExchange(
                    self._account,
                    base_url=base_url,
                    account_address=self._account_address,
                    timeout=sdk_timeout,
                )
                break
            except Exception as e:
                last_exc = e
                if attempt < settings.hl_init_retry_attempts:
                    backoff = settings.hl_init_retry_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "HL init attempt %d/%d failed (%s) — retrying in %.1fs",
                        attempt, settings.hl_init_retry_attempts,
                        type(e).__name__, backoff,
                    )
                    import time as _time
                    _time.sleep(backoff)
        else:
            # All attempts failed; raise a clean error that names HL as
            # the cause so the docker exit log shows the real reason
            # (instead of a noisy SDK stack trace).
            raise RuntimeError(
                f"HyperLiquid API unreachable after "
                f"{settings.hl_init_retry_attempts} attempts — last error: "
                f"{type(last_exc).__name__}: {last_exc}"
            ) from last_exc
        # Bumped from 4 → 16. Even with the SDK timeout above, a long
        # outage where many ticks queue up could still saturate the
        # pool briefly; 16 gives more headroom while still being modest.
        self._executor = ThreadPoolExecutor(max_workers=16)
        # HL price rules: max 5 sig figs AND max (MAX_DECIMALS - szDecimals)
        # decimals (MAX_DECIMALS = 6 for perps). Cache szDecimals per coin
        # so we can round limit_px before submission.
        self._sz_decimals: dict[str, int] = {}
        try:
            meta = self._info.meta()
            for asset in meta.get("universe", []):
                name = asset.get("name")
                sz = asset.get("szDecimals")
                if name and sz is not None:
                    self._sz_decimals[name] = int(sz)
        except Exception:
            logger.exception("Failed to fetch HL meta — price rounding will use fallback")
        logger.info(
            "HyperLiquidExchange initialised (network=%s, signer=%s, account=%s%s)",
            "testnet" if settings.is_testnet else "mainnet",
            self._account.address,
            self._account_address,
            " [API-wallet mode]" if self._account_address.lower() != self._account.address.lower() else "",
        )

    def _round_price(self, symbol: str, px: float) -> float:
        sz_decimals = self._sz_decimals.get(symbol, 4)
        max_decimals = 6  # perp
        # 5 significant figures, then clamp to allowed decimal places
        return round(float(f"{px:.5g}"), max_decimals - sz_decimals)

    def _round_size(self, symbol: str, sz: float) -> float:
        return round(sz, self._sz_decimals.get(symbol, 4))

    def get_size_precision(self, symbol: str) -> int:
        """Return HL's szDecimals for the coin (cached at construction
        from /info meta). Drives the parity-check tolerance per-coin
        so ETH (szDecimals=4) doesn't get the same loose 5e-2 tolerance
        as SOL (szDecimals=2). Audit M4."""
        return self._sz_decimals.get(symbol, 4)

    @property
    def signer_address(self) -> str:
        return self._account.address

    @property
    def address(self) -> str:
        """Trading account address (may differ from signer in API-wallet mode)."""
        return self._account_address

    async def _run(self, fn, *args, timeout: float | None = None, **kwargs):
        """Run a blocking HL SDK call in the thread executor with a
        deadline. Without the timeout, a hung HL API call blocks the
        executor thread indefinitely, which then blocks the runner tick
        (heartbeat stops, risk caps freeze). Audit M2.

        `timeout=None` defaults to settings.hl_read_timeout_seconds. Order
        placement passes a longer explicit timeout via
        settings.hl_order_timeout_seconds. Per-call override allowed for
        special cases (e.g. cancel = short, leverage update = medium).

        Raises asyncio.TimeoutError on deadline; callers wrap as
        appropriate (retry for reads, REJECTED order for writes).
        """
        deadline = (
            timeout if timeout is not None
            else settings.hl_read_timeout_seconds
        )
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                self._executor, lambda: fn(*args, **kwargs)
            ),
            timeout=deadline,
        )

    async def _run_with_retry(self, fn, *args, timeout: float | None = None, **kwargs):
        """Run a HyperLiquid SDK call with retry on transient errors.

        Use ONLY for idempotent read calls (get_positions, get_balance,
        all_mids, user_state, meta). Never wrap order placement — retrying
        a partial order placement risks duplicate fills.

        Each attempt has its own `timeout` (default = read timeout).
        TimeoutError from `_run` IS retryable (counts as transient
        network issue) per the _RETRYABLE_ERRORS tuple including
        TimeoutError.
        """
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                retry=retry_if_exception_type(_RETRYABLE_ERRORS),
                reraise=True,
            ):
                with attempt:
                    if attempt.retry_state.attempt_number > 1:
                        logger.info(
                            "HL retry %d/3 for %s",
                            attempt.retry_state.attempt_number,
                            fn.__name__,
                        )
                    return await self._run(fn, *args, timeout=timeout, **kwargs)
        except RetryError as e:
            raise e.last_attempt.exception() from e

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        is_buy = side == "buy"

        if order_type == OrderType.MARKET:
            mid = await self.get_current_price(symbol)
            if mid <= 0:
                logger.warning("No mid price for %s — cannot place market order", symbol)
                return Order(
                    id=str(uuid.uuid4()),
                    symbol=symbol,
                    side=side,
                    size=size,
                    order_type=order_type,
                    price=price,
                    status=OrderStatus.REJECTED,
                )
            slippage = 0.005  # 0.5% aggressive limit for IOC fill
            limit_px = mid * (1 + slippage) if is_buy else mid * (1 - slippage)
            limit_px = self._round_price(symbol, limit_px)
            tif = "Ioc"
        else:
            limit_px = self._round_price(symbol, float(price or 0))
            tif = "Gtc"

        rounded_size = self._round_size(symbol, float(size))
        if rounded_size <= 0:
            logger.warning(
                "Rounded size for %s is zero (raw=%s, szDecimals=%s) — order skipped",
                symbol, size, self._sz_decimals.get(symbol),
            )
            return Order(
                id=str(uuid.uuid4()),
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                price=price,
                status=OrderStatus.REJECTED,
            )
        try:
            # Order placement gets the longer write timeout. HL match
            # engine can take a few seconds under load; we'd rather
            # accept that than risk a duplicate fill from a too-eager
            # timeout-then-retry loop. Order is NOT in _run_with_retry
            # — a single timed-out attempt becomes REJECTED, not retried.
            result = await self._run(
                self._exchange.order,
                symbol,
                is_buy,
                rounded_size,
                float(limit_px),
                {"limit": {"tif": tif}},
                timeout=settings.hl_order_timeout_seconds,
            )
        except Exception:
            logger.exception("HyperLiquid order failed (or timed out)")
            return Order(
                id=str(uuid.uuid4()),
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                price=price,
                status=OrderStatus.REJECTED,
            )

        status_str = result.get("status", "")
        if status_str != "ok":
            logger.warning("HyperLiquid order rejected: %s", result)
            return Order(
                id=str(uuid.uuid4()),
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                price=price,
                status=OrderStatus.REJECTED,
            )

        statuses = (
            result.get("response", {}).get("data", {}).get("statuses", [])
        )
        order_id = ""
        filled_price = limit_px
        order_status = OrderStatus.REJECTED
        if statuses:
            entry = statuses[0]
            if "filled" in entry:
                f = entry["filled"]
                order_id = str(f.get("oid", ""))
                filled_price = float(f.get("avgPx", limit_px))
                order_status = OrderStatus.FILLED
            elif "resting" in entry:
                order_id = str(entry["resting"].get("oid", ""))
                order_status = OrderStatus.PENDING
            elif "error" in entry:
                logger.warning(
                    "HyperLiquid per-order error for %s %s %s: %s",
                    symbol, side, size, entry["error"],
                )
            else:
                logger.warning(
                    "HyperLiquid unknown status entry for %s %s %s: %s",
                    symbol, side, size, entry,
                )
        else:
            logger.warning(
                "HyperLiquid empty statuses for %s %s %s: %s",
                symbol, side, size, result,
            )

        return Order(
            id=order_id or str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            price=price,
            filled_price=filled_price,
            status=order_status,
        )

    async def update_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> bool:
        try:
            # Write call — use the longer order timeout. update_leverage
            # is also non-retryable (calling it twice is harmless on HL,
            # but timing out and being asked to abort the dependent open
            # is the safer pattern).
            result = await self._run(
                self._exchange.update_leverage,
                int(leverage), symbol, bool(is_cross),
                timeout=settings.hl_order_timeout_seconds,
            )
            ok = result.get("status") == "ok"
            if ok:
                logger.info(
                    "Leverage set: %s %dx (%s)",
                    symbol,
                    leverage,
                    "cross" if is_cross else "isolated",
                )
            else:
                logger.warning("update_leverage rejected for %s: %s", symbol, result)
            return ok
        except Exception:
            logger.exception("Failed to set leverage for %s (or timed out)", symbol)
            return False

    async def cancel_order(self, order_id: str) -> bool:
        try:
            # Cancel uses the order timeout — same reasoning as
            # place_order / update_leverage above.
            await self._run(
                self._exchange.cancel, order_id,
                timeout=settings.hl_order_timeout_seconds,
            )
            return True
        except Exception:
            logger.exception("Cancel order failed")
            return False

    async def _user_state(self) -> dict:
        return await self._run_with_retry(self._info.user_state, self._account_address)

    async def get_positions(self) -> list[Position]:
        try:
            state = await self._user_state()
        except Exception:
            logger.exception("Failed to fetch user state")
            return []

        out: list[Position] = []
        for ap in state.get("assetPositions", []):
            p = ap.get("position", {})
            try:
                szi = float(p.get("szi", "0"))
            except (TypeError, ValueError):
                continue
            # Avoid float == 0 — sub-step residuals from SDK rounding
            # (e.g. 1e-15) would otherwise slip past and create phantom
            # near-zero positions. Audit L1.
            if abs(szi) < 1e-9:
                continue
            out.append(
                Position(
                    symbol=p.get("coin", ""),
                    side="long" if szi > 0 else "short",
                    size=abs(szi),
                    entry_price=float(p.get("entryPx", "0") or 0),
                    unrealized_pnl=float(p.get("unrealizedPnl", "0") or 0),
                    liquidation_price=(
                        float(p["liquidationPx"])
                        if p.get("liquidationPx")
                        else None
                    ),
                )
            )
        return out

    async def get_position(self, symbol: str) -> Position | None:
        positions = await self.get_positions()
        return next((p for p in positions if p.symbol == symbol), None)

    async def get_balance(self) -> Balance:
        try:
            state = await self._user_state()
        except Exception:
            logger.exception("Failed to fetch balance")
            return Balance(total=0, available=0)

        margin = state.get("marginSummary", {})
        total = float(margin.get("accountValue", "0") or 0)
        withdrawable = float(state.get("withdrawable", "0") or 0)
        # unrealized = sum of position pnls
        unrealized = sum(
            float(ap.get("position", {}).get("unrealizedPnl", "0") or 0)
            for ap in state.get("assetPositions", [])
        )
        return Balance(
            total=total,
            available=withdrawable,
            unrealized_pnl=unrealized,
        )

    async def get_user_funding_history(
        self, start_time_ms: int, end_time_ms: int | None = None
    ) -> list[dict]:
        """Fetch funding payments since start_time_ms (epoch ms) for the
        trading account. Returns the raw HL list of funding events."""
        try:
            return await self._run_with_retry(
                self._info.user_funding_history,
                self._account_address,
                start_time_ms,
                end_time_ms,
            ) or []
        except Exception:
            logger.exception("Failed to fetch user funding history")
            return []

    async def get_current_price(self, symbol: str) -> float:
        try:
            mids = await self._run_with_retry(self._info.all_mids)
            return float(mids.get(symbol, 0))
        except Exception:
            logger.exception("Failed to fetch mid price for %s", symbol)
            return 0.0
