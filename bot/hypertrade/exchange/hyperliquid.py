"""Live HyperLiquid exchange implementation.

Uses hyperliquid-python-sdk for real order execution.
Requires HYPERLIQUID_PRIVATE_KEY in .env.

For safety, run with HYPERLIQUID_TESTNET=true first to verify everything
end-to-end against testnet (free testnet USDC from the faucet) before
moving to mainnet.
"""

import asyncio
import logging
import math
import socket
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import requests
from eth_account import Account
from hyperliquid.api import API
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.error import ClientError, ServerError
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from hypertrade.config import settings
from hypertrade.exchange.base import (
    Balance,
    Exchange,
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)

logger = logging.getLogger(__name__)

# Network-level failures that clear on their own: our own
# `asyncio.wait_for` deadline (TimeoutError), DNS, and the SDK's
# `requests.Session` failing to connect or read. requests' exceptions
# subclass OSError, not the builtin ConnectionError / TimeoutError, so
# they have to be named here or a refused connection is never retried.
_TRANSIENT_NETWORK_ERRORS = (
    aiohttp.ClientError,
    socket.gaierror,
    ConnectionError,
    TimeoutError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

# HTTP statuses worth retrying on a READ. The SDK raises `ServerError`
# for >= 500 and `ClientError` for 4xx (`hyperliquid/api.py:
# _handle_exception`), both carrying `.status_code`. Reads are idempotent,
# so a plain 500 is retried along with the gateway / unavailable 5xx and
# request-timeout / rate-limit: giving up on the first 500 made
# `get_current_price` answer 0 and a stop-loss close was REJECTED for a
# whole tick. Everything else (400 validation, 401/403 auth, 422, 501)
# re-fails the same way. Writes never consult this — see `place_order`.
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _is_retryable_server_error(exc: BaseException) -> bool:
    """True when retrying a READ that raised `exc` can help: a transient
    network failure, a malformed 200 (`_MalformedResponseError` is a
    ConnectionError), or an HL HTTP error whose status is in
    `_RETRYABLE_HTTP_STATUSES`.

    The single retry predicate for the constructor's metadata reads and
    for `_run_with_retry`. Writes are never retried — see `place_order`.
    Reads `.status_code` rather than matching digits in the message: the
    message is the response body, and a 501 whose body mentions "502"
    is still a 501.
    """
    if isinstance(exc, (ServerError, ClientError)):
        return getattr(exc, "status_code", None) in _RETRYABLE_HTTP_STATUSES
    return isinstance(exc, _TRANSIENT_NETWORK_ERRORS)


def _order_outcome_unknown(exc: BaseException) -> bool:
    """True when an order POST failed in a way that says nothing about
    whether HL received it: our own deadline, the SDK's socket timeout,
    a dropped connection, or a gateway timeout (504). HL may still have
    filled the order, so `place_order` polls the position (audit H2)
    instead of declaring REJECTED. Any other error is a clear no.
    """
    if isinstance(exc, ServerError):
        return getattr(exc, "status_code", None) == 504
    return isinstance(exc, (
        TimeoutError,
        ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ))


class _MalformedResponseError(ConnectionError):
    """HL answered 200, but not with the shape the call promises.

    The SDK turns a non-JSON body into `{"error": "Could not parse JSON:
    …"}` and returns it as though it were the answer (`hyperliquid/
    api.py:API.post`), so a proxy or CDN error page served with a 200
    comes back as a "successful" read: `get_positions` answered `[]` and
    `get_balance` answered $0 — the #167 flat-book bug through a
    different door. Subclasses ConnectionError on purpose: it is a
    transport failure, not an answer, so the read retry and the runner's
    outage aggregator (`runner._is_transient_network_error`) treat it
    like the 502 it usually stands in for.
    """


class _EmptyMetaError(_MalformedResponseError):
    """`meta` answered, but with no usable perp universe."""


def _require_dict(payload: object, what: str) -> dict:
    if not isinstance(payload, dict) or "error" in payload:
        raise _MalformedResponseError(
            f"{what} returned no usable payload: {str(payload)[:200]}"
        )
    return payload


def _check_user_state(state: object) -> None:
    """`clearinghouseState` always carries both keys, even for an empty
    account — a payload without them is not a flat book."""
    state = _require_dict(state, "user_state")
    if not isinstance(state.get("assetPositions"), list) or not isinstance(
        state.get("marginSummary"), dict
    ):
        raise _MalformedResponseError(
            f"user_state lacks assetPositions/marginSummary: {str(state)[:200]}"
        )


def _check_mids(mids: object) -> None:
    _require_dict(mids, "all_mids")


def _check_list(payload: object) -> None:
    if payload is not None and not isinstance(payload, list):
        raise _MalformedResponseError(
            f"expected a list, got: {str(payload)[:200]}"
        )


def _check_spot_meta(spot_meta: object) -> dict:
    """Enough shape for the SDK's `Info()` constructor to index it."""
    spot_meta = _require_dict(spot_meta, "spotMeta")
    if not isinstance(spot_meta.get("tokens"), list) or not isinstance(
        spot_meta.get("universe"), list
    ):
        raise _MalformedResponseError(
            f"spotMeta lacks tokens/universe: {str(spot_meta)[:200]}"
        )
    return spot_meta


class _ErrorBodyTolerant:
    """Mixin for the SDK's `API._handle_exception`.

    The SDK builds a 4xx `ClientError` from `err["code"]` / `err["msg"]`
    of the JSON body; a 4xx whose JSON body lacks them (a bare 429 from a
    rate limiter, say) raises `KeyError('code')` instead, which no retry
    predicate can recognise as a 429. Re-raise it as the `ClientError`
    the status says it is. Confined to the HTTP layer: only an exception
    escaping `_handle_exception`, which only runs on a >= 400 response.
    Used for reads only; writes keep the stock SDK behaviour.
    """

    def _handle_exception(self, response):
        try:
            super()._handle_exception(response)  # type: ignore[misc]
        except (KeyError, TypeError, AttributeError) as e:
            raise ClientError(
                response.status_code, None, response.text, response.headers,
            ) from e


class _ReadAPI(_ErrorBodyTolerant, API):
    """Bare SDK HTTP client for the constructor's metadata reads."""


class _ReadInfo(_ErrorBodyTolerant, Info):
    """The SDK `Info` used for every read."""


def _parse_sz_decimals(meta: object) -> dict[str, int]:
    """`{coin: szDecimals}` from a `meta` response.

    Raises `_EmptyMetaError` when nothing usable came back — an empty
    universe, or the SDK's `{"error": "Could not parse JSON: …"}` stand-in
    for a non-JSON 200 (a proxy error page, say).
    """
    out: dict[str, int] = {}
    universe = meta.get("universe") if isinstance(meta, dict) else None
    for asset in universe or []:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        sz = asset.get("szDecimals")
        if not name or sz is None:
            continue
        try:
            out[name] = int(sz)
        except (TypeError, ValueError):
            continue
    if not out:
        raise _EmptyMetaError(
            f"meta() returned no usable perp universe: {str(meta)[:200]}"
        )
    return out


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
        # Construct Info + Exchange with retry. Left to itself, the SDK's
        # `Info()` (and the one `HLExchange` builds internally) fetches
        # `meta`/`spot_meta` in its constructor and indexes the answer
        # unchecked — a non-JSON 200 there is a `KeyError: 'tokens'` that
        # no retry predicate recognises. So we fetch both ourselves, once
        # per attempt, validate them, and hand them to both constructors,
        # which then make no network call at all. Without the retry, a
        # transient HL outage during bot start = container exit +
        # restart-loop until HL recovers (witnessed 2026-05-09 — bot was
        # in restart-loop for 4.5h).
        #
        # Only retry on KNOWN transient errors. A retry-on-any-Exception
        # would mask config / programming bugs (TypeError, AttributeError,
        # bad credentials → AuthError) by re-raising them as "HL API
        # unreachable" — much harder to diagnose. Non-transient errors
        # propagate immediately. (PR #24 review fix.)
        #
        # HL price rules: max 5 sig figs AND max (MAX_DECIMALS - szDecimals)
        # decimals (MAX_DECIMALS = 6 for perps); sizes round to szDecimals.
        # The per-coin szDecimals map comes from the same `meta` read. It
        # used to be a separate try/except after construction that logged
        # and carried on with an empty map: every coin then rounded to
        # 4 dp (SOL, szDecimals 2, 422'd on every order) while the bot
        # reported a clean boot. An empty or malformed answer is now
        # retried like a 502 (`_MalformedResponseError`) and then fatal.
        self._sz_decimals: dict[str, int] = {}
        meta_api = _ReadAPI(base_url, timeout=sdk_timeout)
        last_exc: Exception | None = None
        for attempt in range(1, settings.hl_init_retry_attempts + 1):
            try:
                meta = meta_api.post("/info", {"type": "meta", "dex": ""})
                sz_decimals = _parse_sz_decimals(meta)
                spot_meta = _check_spot_meta(
                    meta_api.post("/info", {"type": "spotMeta"})
                )
                self._info = _ReadInfo(
                    base_url, skip_ws=True, meta=meta, spot_meta=spot_meta,
                    timeout=sdk_timeout,
                )
                self._exchange = HLExchange(
                    self._account,
                    base_url=base_url,
                    meta=meta,
                    spot_meta=spot_meta,
                    account_address=self._account_address,
                    timeout=sdk_timeout,
                )
                self._sz_decimals = sz_decimals
                break
            except Exception as e:
                if not _is_retryable_server_error(e):
                    # Non-transient (config bug, auth, programming error)
                    # — re-raise as-is so the operator sees the real
                    # cause, not a misleading "HL unreachable".
                    raise
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
            if isinstance(last_exc, _MalformedResponseError):
                raise RuntimeError(
                    f"HyperLiquid returned no usable metadata after "
                    f"{settings.hl_init_retry_attempts} attempts — refusing "
                    f"to start: without szDecimals every order size and "
                    f"price would be rounded blind. Last answer: {last_exc}"
                ) from last_exc
            raise RuntimeError(
                f"HyperLiquid API unreachable after "
                f"{settings.hl_init_retry_attempts} attempts — last error: "
                f"{type(last_exc).__name__}: {last_exc}"
            ) from last_exc
        meta_api.session.close()  # metadata only; reads use self._info
        # Bumped from 4 → 16. Even with the SDK timeout above, a long
        # outage where many ticks queue up could still saturate the
        # pool briefly; 16 gives more headroom while still being modest.
        self._executor = ThreadPoolExecutor(max_workers=16)
        logger.info(
            "HyperLiquidExchange initialised (network=%s, signer=%s, "
            "account=%s%s, %d perp coins with szDecimals)",
            "testnet" if settings.is_testnet else "mainnet",
            self._account.address,
            self._account_address,
            " [API-wallet mode]" if self._account_address.lower() != self._account.address.lower() else "",
            len(self._sz_decimals),
        )

    # `_round_price` / `_round_size` index `_sz_decimals` directly: an
    # unknown coin is a KeyError, never a guessed 4 dp. `place_order`
    # refuses unknown coins before it gets here.
    def _round_price(self, symbol: str, px: float) -> float:
        sz_decimals = self._sz_decimals[symbol]
        max_decimals = 6  # perp
        # 5 significant figures, then clamp to allowed decimal places
        return round(float(f"{px:.5g}"), max_decimals - sz_decimals)

    def _round_size(self, symbol: str, sz: float) -> float:
        return round(sz, self._sz_decimals[symbol])

    def get_size_precision(self, symbol: str) -> int:
        """Return HL's szDecimals for the coin (cached at construction
        from /info meta). Drives the parity-check tolerance per-coin
        so ETH (szDecimals=4) doesn't get the same loose 5e-2 tolerance
        as SOL (szDecimals=2). Audit M4.

        Only a tolerance, so an unknown coin still gets the base-class
        default — but loudly: no order for that coin can have been
        placed (`place_order` refuses it)."""
        sz = self._sz_decimals.get(symbol)
        if sz is None:
            fallback = super().get_size_precision(symbol)
            logger.warning(
                "No szDecimals for %s in HL meta — parity tolerance falls "
                "back to %d dp", symbol, fallback,
            )
            return fallback
        return sz

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

    async def _run_with_retry(
        self,
        fn,
        *args,
        timeout: float | None = None,
        validate: Callable[[object], object] | None = None,
        **kwargs,
    ):
        """Run a HyperLiquid SDK call with retry on transient errors.

        Use ONLY for idempotent read calls (get_positions, get_balance,
        all_mids, user_state, meta). Never wrap order placement — retrying
        a partial order placement risks duplicate fills.

        Each attempt has its own `timeout` (default = read timeout).
        TimeoutError from `_run` IS retryable (counts as transient
        network issue). What is retried is decided by the
        `_is_retryable_server_error` predicate, the same one the
        constructor uses — it used to be an exception-type tuple, which
        retried every `ServerError` whatever its status and missed
        `requests`' own ConnectionError/Timeout.

        `validate(result)` runs inside each attempt and raises
        `_MalformedResponseError` when a 200 is not the promised shape,
        so a proxy error page is retried like the 502 it stands in for
        instead of being returned as data.
        """
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                retry=retry_if_exception(_is_retryable_server_error),
                reraise=True,
            ):
                with attempt:
                    if attempt.retry_state.attempt_number > 1:
                        logger.info(
                            "HL retry %d/3 for %s",
                            attempt.retry_state.attempt_number,
                            getattr(fn, "__name__", repr(fn)),
                        )
                    result = await self._run(fn, *args, timeout=timeout, **kwargs)
                    if validate is not None:
                        validate(result)
                    return result
        except RetryError as e:
            raise e.last_attempt.exception() from e

    @staticmethod
    def _rejected(
        symbol: str,
        side: str,
        size: float,
        order_type: OrderType,
        price: float | None,
        error: str | None = None,
    ) -> Order:
        return Order(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            price=price,
            status=OrderStatus.REJECTED,
            error=error,
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        *,
        reduce_only: bool = False,
        slippage: float | None = None,
    ) -> Order:
        """Submit one order. Never retried (a retried write can fill twice).

        `reduce_only` goes to HL as the order's `r` flag: HL fills at most
        the position it reduces and rejects one that would increase or
        open a position ("Reduce only order would increase position.",
        an `error` status → REJECTED). `slippage` widens or narrows the
        IOC band of a MARKET order (default 0.5 %).

        `Order.size` is what HL actually took, not what was asked for: the
        filled `totalSz` when HL reports it, else the szDecimals-rounded
        size that was submitted. The requested size is never on the
        exchange — 0.002523 BTC is sent (and filled) as 0.00252 — so
        booking it puts DB and exchange apart from the first fill
        (`bot/reports/analysis-2026-09-15.md` § 4).
        """
        is_buy = side == "buy"

        # Refuse a coin we have no szDecimals for rather than guess. The
        # old 4 dp fallback sent SOL (szDecimals 2) at 4 dp and HL 422'd it.
        if symbol not in self._sz_decimals:
            logger.error(
                "Order REFUSED for %s %s %s: %s has no szDecimals in HL meta "
                "(unknown or delisted coin?) — cannot round size or price",
                symbol, side, size, symbol,
            )
            return self._rejected(symbol, side, size, order_type, price, "no szDecimals")

        if order_type == OrderType.MARKET:
            mid = await self.get_current_price(symbol)
            if mid <= 0:
                logger.warning(
                    "No mid price for %s — cannot place market order %s %s",
                    symbol, side, size,
                )
                return self._rejected(symbol, side, size, order_type, price, "no mid price")
            if slippage is None:
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
                symbol, size, self._sz_decimals[symbol],
            )
            return self._rejected(symbol, side, size, order_type, price, "size rounds to 0")
        # Audit H2: snapshot the pre-order signed position size so a
        # post-timeout poll can detect a delayed fill. Long = +size,
        # short = -size. Best-effort — if the read fails, we lose the
        # ability to detect a delayed fill on timeout but the order
        # path itself proceeds normally.
        try:
            pre_signed = await self._signed_position_size(symbol)
        except Exception:
            pre_signed = None
        sent_ms = int(time.time() * 1000)
        try:
            # Order placement gets the longer write timeout. HL match
            # engine can take a few seconds under load; we'd rather
            # accept that than risk a duplicate fill from a too-eager
            # timeout-then-retry loop. Order is NOT in _run_with_retry
            # — a single timed-out attempt risks duplicate fills.
            result = await self._run(
                self._exchange.order,
                symbol,
                is_buy,
                rounded_size,
                float(limit_px),
                {"limit": {"tif": tif}},
                reduce_only=reduce_only,
                timeout=settings.hl_order_timeout_seconds,
            )
        except Exception as e:
            if not _order_outcome_unknown(e):
                logger.exception(
                    "HyperLiquid order failed for %s %s %s @ %s",
                    symbol, side, rounded_size, limit_px,
                )
                return self._rejected(symbol, side, rounded_size, order_type, price, repr(e))
            # The POST timed out or lost its connection — but HL may
            # STILL fill the order. Returning REJECTED here was the audit
            # H2 bug: the runner would skip the DB write while the
            # exchange built up a position. Reconcile catches it 5 min
            # later, but during those 5 min the strategy can't manage the
            # position. Poll for ~30s; if a new position appears, treat
            # it as filled at the observed entry price. This covers the
            # SDK's own `requests` timeout as well as our `wait_for`
            # deadline — the SDK is given the same deadline, so either
            # can fire first.
            logger.warning(
                "HyperLiquid order outcome UNKNOWN for %s %s %s @ %s "
                "(%s: %s) — polling for delayed fill before declaring "
                "REJECTED",
                symbol, side, rounded_size, limit_px, type(e).__name__, e,
            )
            return await self._poll_for_delayed_fill(
                symbol=symbol,
                side=side,
                requested_size=size,
                requested_rounded=rounded_size,
                order_type=order_type,
                price=price,
                pre_signed=pre_signed,
                limit_px=limit_px,
                reduce_only=reduce_only,
                sent_ms=sent_ms,
            )

        status_str = result.get("status", "")
        if status_str != "ok":
            logger.warning(
                "HyperLiquid order rejected for %s %s %s: %s",
                symbol, side, rounded_size, result,
            )
            return self._rejected(
                symbol, side, rounded_size, order_type, price, str(result)[:200],
            )

        statuses = (
            result.get("response", {}).get("data", {}).get("statuses", [])
        )
        order_id = ""
        filled_price = limit_px
        filled_size = rounded_size
        order_status = OrderStatus.REJECTED
        error = "no order status"
        if statuses:
            entry = statuses[0]
            if "filled" in entry:
                f = entry["filled"]
                order_id = str(f.get("oid", ""))
                filled_price = float(f.get("avgPx", limit_px))
                filled_size = self._filled_size(
                    f, symbol=symbol, side=side, submitted=rounded_size,
                    requested=size, filled_price=filled_price,
                )
                if filled_size > 0:
                    order_status, error = OrderStatus.FILLED, None
                else:
                    # HL said "filled" with a readable size of zero (or
                    # less): nothing is on the exchange, so nothing may
                    # be booked. Reporting it FILLED at the submitted
                    # size would put a phantom position in the DB.
                    logger.warning(
                        "HyperLiquid reported a fill of %s for %s %s %s "
                        "(oid=%s) — treating as REJECTED",
                        filled_size, symbol, side, rounded_size,
                        order_id or "?",
                    )
                    filled_size = rounded_size
                    error = "filled 0"
            elif "resting" in entry:
                oid = entry["resting"].get("oid")
                order_id = str(oid) if oid is not None else ""
                order_status = await self._cancel_resting(
                    symbol=symbol, side=side, size=rounded_size,
                    limit_px=limit_px, tif=tif, order_id=order_id,
                )
                error = "rested on the book instead of filling"
            elif "error" in entry:
                logger.warning(
                    "HyperLiquid per-order error for %s %s %s: %s",
                    symbol, side, rounded_size, entry["error"],
                )
                error = str(entry["error"])
            else:
                logger.warning(
                    "HyperLiquid unknown status entry for %s %s %s: %s",
                    symbol, side, rounded_size, entry,
                )
        else:
            logger.warning(
                "HyperLiquid empty statuses for %s %s %s: %s",
                symbol, side, rounded_size, result,
            )

        return Order(
            id=order_id or str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            size=filled_size,
            order_type=order_type,
            price=price,
            filled_price=filled_price,
            status=order_status,
            error=error,
        )

    @staticmethod
    def _filled_size(
        filled: dict,
        *,
        symbol: str,
        side: str,
        submitted: float,
        requested: float,
        filled_price: float,
    ) -> float:
        """The size HL reports as filled (`totalSz`), else what we sent.

        An IOC order can fill partly and cancel the rest; `totalSz` is the
        only place that shows it. A readable value is returned as-is,
        including 0 or less — the caller turns that into REJECTED. Only
        when the field is absent or unparseable (or not finite) is the
        submitted (rounded) size the best remaining answer, never the
        unrounded request.
        """
        try:
            total = float(filled.get("totalSz"))
        except (TypeError, ValueError):
            total = None
        if total is None or not math.isfinite(total):
            logger.warning(
                "HyperLiquid fill for %s %s has no usable totalSz (%r) — "
                "booking the submitted size %s",
                symbol, side, filled.get("totalSz"), submitted,
            )
            return submitted
        if total > 0 and abs(total - submitted) > 1e-9:
            logger.warning(
                "HyperLiquid %s for %s %s: requested %s, submitted %s, "
                "filled %s @ %s — booking the filled size",
                "PARTIAL fill" if total < submitted else "OVER-fill",
                symbol, side, requested, submitted, total, filled_price,
            )
        return total

    async def _cancel_resting(
        self,
        *,
        symbol: str,
        side: str,
        size: float,
        limit_px: float,
        tif: str,
        order_id: str,
    ) -> OrderStatus:
        """Cancel an order HL left resting on the book.

        The engine books only immediate fills: a non-FILLED order gets no
        DB row, so a resting order that fills later is an exchange
        position nobody owns. Every order today is an IOC market order,
        which cannot rest — this is the guard for the first GTC limit
        order someone adds. Returns CANCELLED, or PENDING when the cancel
        could not be confirmed (it may already have filled).

        Known gap, left for whoever adds limit orders: a GTC order can
        fill partly and rest the remainder, and HL's `resting` status
        does not say how much filled first. That part is booked nowhere
        here — reconcile sees it as an exchange orphan. A limit-order
        caller must read the fills (`fetch_user_fills`) for the oid.
        """
        logger.warning(
            "HyperLiquid order for %s %s %s @ %s is RESTING (tif=%s, oid=%s) "
            "— cancelling it: the engine only books immediate fills",
            symbol, side, size, limit_px, tif, order_id or "?",
        )
        if order_id and await self.cancel_order(order_id, symbol):
            return OrderStatus.CANCELLED
        logger.error(
            "HyperLiquid resting order for %s %s %s @ %s (oid=%s) could NOT "
            "be cancelled — if it fills it has no DB row; reconcile will "
            "treat it as an exchange orphan",
            symbol, side, size, limit_px, order_id or "?",
        )
        return OrderStatus.PENDING

    async def _signed_position_size(self, symbol: str) -> float:
        """Return the current position size for `symbol` with sign:
        positive for long, negative for short, 0 for flat. Used as the
        baseline for the audit-H2 timeout-poll path so we can detect a
        delayed fill regardless of trade direction."""
        pos = await self.get_position(symbol)
        if pos is None:
            return 0.0
        return float(pos.size) if pos.side == "long" else -float(pos.size)

    async def _poll_for_delayed_fill(
        self,
        *,
        symbol: str,
        side: str,
        requested_size: float,
        requested_rounded: float,
        order_type: OrderType,
        price: float | None,
        pre_signed: float | None,
        limit_px: float,
        reduce_only: bool = False,
        sent_ms: int | None = None,
    ) -> Order:
        """Audit H2: poll the exchange for a delayed fill after a
        place_order timeout.

        We can't tell from the SDK whether the order made it to HL's
        match engine. If pre-order signed-size was X and post-order is
        X ± requested_rounded (within a per-asset rounding tolerance),
        the order DID fill — return Order(FILLED) with
        `size=requested_rounded`, the amount that was actually sent (the
        unrounded request never reaches HL). Otherwise we treat it as
        truly REJECTED.

        If `pre_signed` is None (the pre-order baseline read failed),
        we degrade to REJECTED — better to surface the timeout than
        misclassify an unrelated existing position as a fresh fill.

        A reduce-only order fills at most the position it reduces, so the
        poll waits for that clamped amount; with nothing to reduce it
        cannot have filled at all. What is left of the position says
        nothing about what a close filled at: it is priced from HL's own
        closing fills since `sent_ms`, else the mid (`_late_close_price`).
        """
        if pre_signed is None:
            logger.warning(
                "Timeout-poll skipped (no pre-order baseline) — "
                "returning REJECTED for %s %s %s",
                symbol, side, requested_rounded,
            )
            return self._rejected(
                symbol, side, requested_rounded, order_type, price,
                "outcome unknown (timeout, no pre-order position read)",
            )

        # Tolerance: one min step at szDecimals precision, matching the
        # parity-check rule in runner.py:_check_parity_after_trade.
        sz_dec = self._sz_decimals[symbol]
        tolerance = max(10 ** (-sz_dec), 1e-9)
        expected = requested_rounded
        if reduce_only:
            reducible = -pre_signed if side == "buy" else pre_signed
            expected = min(requested_rounded, max(reducible, 0.0))
            if expected < tolerance:
                logger.warning(
                    "Timeout-poll skipped — reduce-only %s %s %s had no "
                    "position to reduce (pre-order %s); returning REJECTED",
                    symbol, side, requested_rounded, pre_signed,
                )
                return self._rejected(
                    symbol, side, requested_rounded, order_type, price,
                    "timeout, and no position to reduce",
                )
        expected_delta = expected if side == "buy" else -expected
        target = pre_signed + expected_delta

        # Poll up to ~30s. Bursty at first to catch quick fills, then
        # backs off so we don't hammer HL during a wider outage.
        delays = [1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 5.0, 5.0, 8.0]
        elapsed = 0.0
        for delay in delays:
            await asyncio.sleep(delay)
            elapsed += delay
            try:
                current = await self._signed_position_size(symbol)
            except Exception:
                logger.warning(
                    "Timeout-poll: read failed at %.0fs (will keep polling)",
                    elapsed,
                )
                continue
            if abs(current - target) <= tolerance:
                # Match — the order filled while we were waiting.
                # Read the actual entry from the position so the runner
                # records the real fill price, not our limit_px guess.
                try:
                    pos = await self.get_position(symbol)
                    fill_px = float(pos.entry_price) if pos else float(limit_px)
                except Exception:
                    fill_px = float(limit_px)
                oid = None
                if reduce_only:  # what is left says nothing of a close's price
                    fill_px, oid = await self._late_close_price(
                        symbol, side, expected, sent_ms, limit_px,
                    )
                logger.warning(
                    "Timeout-poll: detected delayed fill for %s %s %s "
                    "(requested %s) after %.0fs (price≈%.4f). Treating as "
                    "FILLED.",
                    symbol, side, requested_rounded, requested_size,
                    elapsed, fill_px,
                )
                return Order(
                    id=oid or str(uuid.uuid4()), symbol=symbol, side=side,
                    size=expected, order_type=order_type,
                    price=price, filled_price=fill_px,
                    status=OrderStatus.FILLED,
                )
        logger.error(
            "Timeout-poll: no delayed fill for %s %s %s after %.0fs — "
            "returning REJECTED. Reconcile will catch it if HL fills late.",
            symbol, side, requested_rounded, elapsed,
        )
        return self._rejected(
            symbol, side, requested_rounded, order_type, price,
            f"outcome unknown: timed out, no fill seen in {elapsed:.0f}s "
            "(it may still fill late)",
        )

    async def _late_close_price(
        self, symbol: str, side: str, size: float, sent_ms: int | None,
        limit_px: float,
    ) -> tuple[float, str | None]:
        """The price and oid of a close that filled after its POST timed
        out: HL's closing fills for `size` since it was sent (less a
        little clock skew). Else the mid, logged as an estimate; the limit
        price (as far as 5 % off) only when there is no mid either."""
        from hypertrade.reconcile.fills import FillLedger  # noqa: PLC0415

        since = None if sent_ms is None else sent_ms - 5_000
        try:
            got = FillLedger(await self.fetch_user_fills(since_ms=since)).take(
                symbol=symbol, size=size, since_ms=since,
                position_side="long" if side == "sell" else "short",
            )
            if got is not None:
                return got.price, got.order_id
            mid = await self.get_current_price(symbol)  # 0.0 on error
        except Exception:
            logger.exception("Timeout-poll: pricing the late close failed")
            mid = 0.0
        logger.warning(
            "Timeout-poll: no closing fill found for %s %s %s — priced "
            "ESTIMATED at %s", symbol, side, size,
            f"the mid {mid}" if mid > 0 else f"the limit {limit_px}",
        )
        return (mid if mid > 0 else float(limit_px)), None

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

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel one resting order. True only when HL confirms it.

        Two things used to make this lie. It returned True without
        looking at the answer, and HL answers a cancel for an order that
        already filled or never existed with top-level `"status": "ok"`
        and the failure inside `data.statuses`. And it called the SDK as
        `cancel(order_id)` while the SDK's signature is `cancel(coin,
        oid)` — every call was a TypeError swallowed as False.
        """
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            logger.error(
                "Cancel REFUSED for %s: %r is not a HyperLiquid order id",
                symbol, order_id,
            )
            return False
        try:
            # Cancel uses the order timeout — same reasoning as
            # place_order / update_leverage above. Not retried: it is a
            # write.
            result = await self._run(
                self._exchange.cancel, symbol, oid,
                timeout=settings.hl_order_timeout_seconds,
            )
        except Exception:
            logger.exception(
                "Cancel failed for %s oid=%s (or timed out)", symbol, oid,
            )
            return False

        if not isinstance(result, dict) or result.get("status") != "ok":
            logger.warning(
                "HyperLiquid cancel rejected for %s oid=%s: %s",
                symbol, oid, result,
            )
            return False
        response = result.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not statuses or any(s != "success" for s in statuses):
            logger.warning(
                "HyperLiquid cancel NOT confirmed for %s oid=%s: %s",
                symbol, oid, statuses if statuses else result,
            )
            return False
        logger.info("Cancelled HyperLiquid order %s oid=%s", symbol, oid)
        return True

    async def _user_state(self) -> dict:
        """`clearinghouseState` for the trading account, shape-checked:
        a 200 without `assetPositions`/`marginSummary` (a proxy error
        page, parsed by the SDK into `{"error": …}`) raises instead of
        reading as a flat, empty account."""
        return await self._run_with_retry(
            self._info.user_state, self._account_address,
            validate=_check_user_state,
        )

    async def get_positions(self) -> list[Position]:
        """Open positions, or ExchangeReadError if HL could not answer.

        Never returns `[]` for a failed read. `[]` used to mean both
        "flat" and "502 Bad Gateway"; reconcile could not tell them
        apart and closed the whole book on the second one
        (`bot/reports/analysis-2026-09-15.md` § 2).
        """
        try:
            state = await self._user_state()
        except Exception as e:
            logger.exception("Failed to fetch user state")
            raise ExchangeReadError(f"get_positions failed: {e}") from e

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
        """One coin's position. Propagates ExchangeReadError."""
        positions = await self.get_positions()
        return next((p for p in positions if p.symbol == symbol), None)

    async def get_balance(self) -> Balance:
        """Account balance, or ExchangeReadError if HL could not answer.

        A zeroed Balance used to be returned on failure, which wrote a
        0-equity snapshot into `equity_snapshots` and made the drawdown
        / kill-switch maths see a blown account.
        """
        try:
            state = await self._user_state()
        except Exception as e:
            logger.exception("Failed to fetch balance")
            raise ExchangeReadError(f"get_balance failed: {e}") from e

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
                validate=_check_list,
            ) or []
        except Exception:
            logger.exception("Failed to fetch user funding history")
            return []

    async def fetch_user_fills(
        self, address: str | None = None, since_ms: int | None = None,
    ) -> list[dict]:
        """Return raw HL fill records for the trading account.

        When `since_ms` is provided, uses the SDK's time-bounded
        `user_fills_by_time(address, start_time)`. Without it, returns
        the recent default window from `user_fills(address)`. Empty
        list on any error so the reconcile caller can continue.
        """
        addr = (address or self._account_address)
        try:
            if since_ms is not None:
                return await self._run_with_retry(
                    self._info.user_fills_by_time, addr, int(since_ms),
                    validate=_check_list,
                ) or []
            return await self._run_with_retry(
                self._info.user_fills, addr, validate=_check_list,
            ) or []
        except Exception:
            logger.exception("Failed to fetch user fills for %s", addr)
            return []

    async def get_current_price(self, symbol: str) -> float:
        try:
            mids = await self._run_with_retry(
                self._info.all_mids, validate=_check_mids,
            )
            return float(mids.get(symbol, 0))
        except Exception:
            logger.exception("Failed to fetch mid price for %s", symbol)
            return 0.0
