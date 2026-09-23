"""HyperLiquid exchange-wrapper hardening (analysis-2026-09-15 § 4).

1. `Order.size` is what HL took (filled `totalSz`, else the szDecimals-
   rounded size that was submitted), never the unrounded request. The
   request was what the runner booked, which is the live 247x
   "BTC size mismatch — DB total 0.002523 vs exchange 0.002520" warning.
   A readable `totalSz` of 0 is REJECTED, not a full fill.
2. A failed, empty or non-JSON `meta`/`spotMeta` at construction is
   retried and then fatal, and an order for a coin with no szDecimals is
   refused — no silent 4 dp fallback.
3. Reads retry transient network errors, malformed 200s and 408/429/
   500/502/503/504; any other 4xx/5xx fails on the first attempt. Writes
   never retry.
4. `cancel_order` calls the SDK with its real signature and only reports
   success when HL confirms it.
5. An order HL leaves resting is cancelled, not silently left to fill.
6. A non-JSON 200 on `user_state` raises ExchangeReadError instead of
   reading as a flat book / $0 balance.
7. An order POST whose outcome is unknown (SDK socket timeout, dropped
   connection, 504) gets the audit-H2 delayed-fill poll, not REJECTED.

Unit tests `create_autospec` the SDK objects from the real classes, so a
call with the wrong arity fails here instead of in production. Tests
that need the SDK's own HTTP and JSON handling use the real classes with
`requests.Session.post` faked (`hl_http`, conftest.py).
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
import requests
from hyperliquid.exchange import Exchange as HLExchange
from hyperliquid.info import Info
from hyperliquid.utils.error import ClientError, ServerError

from hypertrade.exchange import hyperliquid as hl_module
from hypertrade.exchange.base import ExchangeReadError, OrderStatus, OrderType
from hypertrade.exchange.hyperliquid import (
    HyperLiquidExchange,
    _is_retryable_server_error,
    _order_outcome_unknown,
)

from .conftest import EMPTY_ACCOUNT, HTML_PAGE, META, SPOT_META


@pytest.fixture
def ex():
    with patch.object(HyperLiquidExchange, "__init__", return_value=None):
        exchange = HyperLiquidExchange()
    exchange._account = MagicMock()
    exchange._account_address = "0xabc"
    exchange._info = create_autospec(Info, instance=True)
    exchange._exchange = create_autospec(HLExchange, instance=True)
    exchange._executor = ThreadPoolExecutor(max_workers=2)
    exchange._sz_decimals = {"BTC": 5, "ETH": 4, "SOL": 2}
    exchange._info.all_mids.return_value = {"BTC": "50000.0", "SOL": "150.0"}
    exchange._info.user_state.return_value = EMPTY_ACCOUNT
    try:
        yield exchange
    finally:
        exchange._executor.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip tenacity's backoff and the timeout-poll delays."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


def _filled(oid=101, avg_px="50010.0", total_sz: str | None = "0.00252"):
    filled = {"oid": oid, "avgPx": avg_px}
    if total_sz is not None:
        filled["totalSz"] = total_sz
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"filled": filled}]}},
    }


# --- 1. Order.size is the size HL took --------------------------------------


async def test_fill_reports_the_rounded_size_not_the_request(ex):
    """The report's case: 0.002523 BTC at szDecimals 5 goes out as
    0.00252, and that — not 0.002523 — is what the runner must book."""
    ex._exchange.order.return_value = _filled(total_sz="0.00252")

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)
    sent_size = ex._exchange.order.call_args.args[2]
    assert sent_size == pytest.approx(0.00252, abs=1e-12)


async def test_fill_without_total_sz_falls_back_to_the_submitted_size(ex):
    """If HL ever omits totalSz, the next-best truth is what was sent —
    still never the unrounded request."""
    ex._exchange.order.return_value = _filled(total_sz=None)

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)


async def test_partial_ioc_fill_reports_the_filled_size(ex, caplog):
    ex._exchange.order.return_value = _filled(total_sz="0.0012")

    with caplog.at_level(logging.WARNING, logger=hl_module.__name__):
        order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.0012, abs=1e-12)
    assert "PARTIAL fill for BTC buy" in caplog.text


async def test_sell_is_rounded_the_same_way(ex):
    ex._exchange.order.return_value = _filled(total_sz="1.23")

    order = await ex.place_order("SOL", "sell", 1.234567)

    assert order.size == pytest.approx(1.23, abs=1e-12)
    assert ex._exchange.order.call_args.args[2] == pytest.approx(1.23, abs=1e-12)


async def test_delayed_fill_after_timeout_reports_the_rounded_size(ex, no_sleep):
    """The audit-H2 poll path used to hand back `requested_size`."""
    ex._signed_position_size = AsyncMock(return_value=0.00252)
    ex.get_position = AsyncMock(return_value=None)

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="buy", requested_size=0.002523,
        requested_rounded=0.00252, order_type=OrderType.MARKET,
        price=None, pre_signed=0.0, limit_px=50_250.0,
    )

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)


async def test_timeout_then_poll_through_place_order_reports_rounded_size(
    ex, no_sleep,
):
    """End to end through `place_order`: the SDK times out, the poll sees
    the position appear, and the Order carries the submitted size."""
    ex._exchange.order.side_effect = asyncio.TimeoutError()
    ex._signed_position_size = AsyncMock(side_effect=[0.0, 0.00252])
    ex.get_position = AsyncMock(return_value=None)

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)


# --- 2. szDecimals: retried at boot, fatal when missing, no 4 dp guess ------
#
# Real SDK `Info` / `Exchange` constructors, HTTP faked (conftest.py). A
# MagicMock `Info` hid that the stock SDK constructor fetches `spotMeta`
# itself and indexes it unchecked: a non-JSON 200 there was a
# `KeyError: 'tokens'` on attempt 1 that nothing retried.


def test_meta_fetch_is_retried_at_construction(hl_http, build_exchange):
    """A 503 on the meta read used to be logged and ignored, leaving an
    empty szDecimals map. It is now retried inside the init loop."""
    hl_http.route("meta", (503, "Service Unavailable"), (200, META))

    exchange = build_exchange(attempts=3)

    assert exchange._sz_decimals == {"BTC": 5, "ETH": 4, "SOL": 2}
    assert hl_http.count("meta") == 2


def test_constructors_reuse_our_metadata_and_make_no_request(
    hl_http, build_exchange,
):
    """The validated meta/spotMeta go into both SDK constructors, so the
    only metadata requests are ours — one each."""
    exchange = build_exchange()

    assert isinstance(exchange._info, hl_module._ReadInfo)
    assert hl_http.calls == ["meta", "spotMeta"]
    # The SDK Exchange's own Info was built from our meta: it can resolve
    # the coin an order is for without a network call.
    assert exchange._exchange.info.name_to_asset("SOL") == 2


def test_construction_fails_when_meta_stays_empty(hl_http, build_exchange):
    """Booting with no szDecimals is how SOL orders went out at 4 dp and
    422'd while the bot looked healthy. Refuse to start instead."""
    hl_http.route("meta", (200, {"universe": []}))

    with pytest.raises(RuntimeError, match="no usable metadata"):
        build_exchange(attempts=3)
    assert hl_http.count("meta") == 3


def test_non_json_meta_200_is_retried_then_fatal(hl_http, build_exchange):
    """A proxy error page served with a 200: the SDK returns `{"error":
    "Could not parse JSON: …"}` as if it were the answer."""
    hl_http.route("meta", (200, HTML_PAGE))

    with pytest.raises(RuntimeError, match="no usable metadata"):
        build_exchange(attempts=3)
    assert hl_http.count("meta") == 3


def test_non_json_spot_meta_200_is_retried_then_fatal(hl_http, build_exchange):
    """The review's case: with the stock SDK constructor this was a
    `KeyError: 'tokens'` on attempt 1, never retried."""
    hl_http.route("spotMeta", (200, HTML_PAGE))

    with pytest.raises(RuntimeError, match="no usable metadata"):
        build_exchange(attempts=3)
    assert hl_http.count("spotMeta") == 3


def test_non_json_spot_meta_recovers_on_retry(hl_http, build_exchange):
    hl_http.route("spotMeta", (200, HTML_PAGE), (200, SPOT_META))

    exchange = build_exchange(attempts=3)

    assert exchange._sz_decimals["BTC"] == 5
    assert hl_http.count("spotMeta") == 2


def test_construction_fails_when_meta_stays_unreachable(hl_http, build_exchange):
    hl_http.route("meta", (502, "Bad Gateway"))

    with pytest.raises(RuntimeError, match="HyperLiquid API unreachable"):
        build_exchange(attempts=3)
    assert hl_http.count("meta") == 3


def test_non_transient_meta_error_is_not_retried(hl_http, build_exchange):
    hl_http.route("meta", (401, {"code": 401, "msg": "unauthorized"}))

    with pytest.raises(ClientError):
        build_exchange(attempts=5)
    assert hl_http.count("meta") == 1


def test_malformed_universe_entries_are_skipped():
    assert hl_module._parse_sz_decimals({"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "", "szDecimals": 3},
        {"name": "BAD", "szDecimals": "x"},
        {"name": "NOSZ"},
        "junk",
    ]}) == {"BTC": 5}


async def test_order_for_a_coin_without_sz_decimals_is_refused(ex, caplog):
    """No guessing 4 dp: the order never reaches HL, and the log says why."""
    with caplog.at_level(logging.ERROR, logger=hl_module.__name__):
        order = await ex.place_order("DOGE", "buy", 100.0)

    assert order.status == OrderStatus.REJECTED
    ex._exchange.order.assert_not_called()
    assert "DOGE has no szDecimals" in caplog.text


async def test_rounding_helpers_never_guess_for_unknown_coins(ex):
    with pytest.raises(KeyError):
        ex._round_size("DOGE", 1.23456)
    with pytest.raises(KeyError):
        ex._round_price("DOGE", 0.123456)


def test_size_precision_for_unknown_coin_warns(ex, caplog):
    with caplog.at_level(logging.WARNING, logger=hl_module.__name__):
        assert ex.get_size_precision("SOL") == 2
        assert ex.get_size_precision("DOGE") == 4
    assert "No szDecimals for DOGE" in caplog.text


# --- 3. Read retry: transient + retryable statuses; writes never ----------


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (ServerError(502, "Bad Gateway"), True),
        (ServerError(503, "Service Unavailable"), True),
        (ServerError(504, "Gateway Timeout"), True),
        (ServerError(500, "Internal Server Error"), True),
        (ServerError(501, "upstream said 502 earlier"), False),
        (ServerError(505, "HTTP Version Not Supported"), False),
        (ServerError(400, "Bad Request"), False),
        (ServerError(422, "Unprocessable"), False),
        (ClientError(429, None, "rate limited", None), True),
        (ClientError(408, None, "request timeout", None), True),
        (ClientError(400, None, "bad request", None), False),
        (ClientError(422, None, "invalid size", None), False),
        (TimeoutError(), True),
        (asyncio.TimeoutError(), True),
        (ConnectionError("reset"), True),
        (requests.exceptions.ConnectionError("refused"), True),
        (requests.exceptions.ReadTimeout("slow"), True),
        (hl_module._MalformedResponseError("html 200"), True),
        (ValueError("bug"), False),
        (KeyError("universe"), False),
    ],
)
def test_retry_predicate(exc, retryable):
    assert _is_retryable_server_error(exc) is retryable


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_read_retries_a_5xx_until_it_succeeds(ex, no_sleep, status):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServerError(status, "flaky")
        return {"BTC": "50000.0"}

    assert await ex._run_with_retry(flaky) == {"BTC": "50000.0"}
    assert calls["n"] == 3


async def test_price_read_survives_a_plain_500(hl_http, build_exchange, no_sleep):
    """The review's case: a single 500 on allMids used to make
    `get_current_price` answer 0, and a stop-loss market close was
    REJECTED for the whole tick."""
    exchange = build_exchange()
    hl_http.route(
        "allMids", (500, "Internal Server Error"), (200, {"BTC": "50000.5"}),
    )

    assert await exchange.get_current_price("BTC") == pytest.approx(50_000.5)
    assert hl_http.count("allMids") == 2


@pytest.mark.parametrize(
    "exc",
    [
        ServerError(400, "Bad Request"),
        ServerError(501, "Not Implemented"),
        ClientError(422, None, "invalid", None),
    ],
)
async def test_read_does_not_retry_a_permanent_http_error(ex, no_sleep, exc):
    calls = {"n": 0}

    def fails():
        calls["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        await ex._run_with_retry(fails)
    assert calls["n"] == 1


@pytest.mark.parametrize("status", [500, 503])
async def test_order_write_is_never_retried(ex, no_sleep, status):
    """A status a read would retry must not resubmit an order: the first
    attempt may have reached the matching engine."""
    ex._exchange.order.side_effect = ServerError(status, "flaky")

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.REJECTED
    assert ex._exchange.order.call_count == 1


async def test_cancel_write_is_never_retried(ex, no_sleep):
    ex._exchange.cancel.side_effect = ServerError(503, "Service Unavailable")

    assert await ex.cancel_order("77", "BTC") is False
    assert ex._exchange.cancel.call_count == 1


# --- 4. cancel_order checks what HL answered --------------------------------


async def test_cancel_confirmed_by_hl_returns_true(ex):
    ex._exchange.cancel.return_value = {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": ["success"]}},
    }

    assert await ex.cancel_order("77", "BTC") is True
    ex._exchange.cancel.assert_called_once_with("BTC", 77)


async def test_cancel_of_filled_or_unknown_order_returns_false(ex, caplog):
    """HL says top-level "ok" and puts the failure in the statuses."""
    ex._exchange.cancel.return_value = {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": [
            {"error": "Order was never placed, already canceled, or filled. asset=0"},
        ]}},
    }

    with caplog.at_level(logging.WARNING, logger=hl_module.__name__):
        assert await ex.cancel_order("77", "BTC") is False
    assert "cancel NOT confirmed for BTC oid=77" in caplog.text


async def test_cancel_top_level_error_returns_false(ex):
    ex._exchange.cancel.return_value = {"status": "err", "response": "User or API Wallet does not exist."}

    assert await ex.cancel_order("77", "BTC") is False


async def test_cancel_with_empty_statuses_returns_false(ex):
    ex._exchange.cancel.return_value = {
        "status": "ok", "response": {"type": "cancel", "data": {"statuses": []}},
    }

    assert await ex.cancel_order("77", "BTC") is False


async def test_cancel_refuses_a_non_hl_order_id(ex):
    """Order ids we minted ourselves (uuid4 on REJECTED) are not oids."""
    assert await ex.cancel_order("5c0f3e3a-not-an-oid", "BTC") is False
    ex._exchange.cancel.assert_not_called()


# --- 5. A resting order is cancelled, not left to fill untracked ------------


def _resting(oid=4242):
    return {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}},
    }


async def test_resting_order_is_cancelled(ex, caplog):
    ex._exchange.order.return_value = _resting(4242)
    ex._exchange.cancel.return_value = {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": ["success"]}},
    }

    with caplog.at_level(logging.WARNING, logger=hl_module.__name__):
        order = await ex.place_order(
            "BTC", "buy", 0.002523, OrderType.LIMIT, price=49_000.0,
        )

    ex._exchange.cancel.assert_called_once_with("BTC", 4242)
    assert order.status == OrderStatus.CANCELLED
    assert order.id == "4242"
    assert order.size == pytest.approx(0.00252, abs=1e-12)
    assert "is RESTING" in caplog.text


async def test_resting_order_that_cannot_be_cancelled_stays_pending(ex, caplog):
    """If the cancel is not confirmed the order may still fill. Say so
    loudly; the runner books nothing for a non-FILLED order."""
    ex._exchange.order.return_value = _resting(4242)
    ex._exchange.cancel.return_value = {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": [
            {"error": "Order was never placed, already canceled, or filled."},
        ]}},
    }

    with caplog.at_level(logging.ERROR, logger=hl_module.__name__):
        order = await ex.place_order(
            "BTC", "buy", 0.002523, OrderType.LIMIT, price=49_000.0,
        )

    assert order.status == OrderStatus.PENDING
    assert "could NOT be cancelled" in caplog.text


# --- 6. A readable totalSz of 0 is REJECTED, not a full fill -----------------


@pytest.mark.parametrize("total_sz", ["0.0", "0", "-0.001"])
async def test_readable_zero_fill_is_rejected(ex, caplog, total_sz):
    """A `filled` entry that says it filled nothing used to be reported
    FILLED at the full submitted size — a phantom position in the DB."""
    ex._exchange.order.return_value = _filled(total_sz=total_sz)

    with caplog.at_level(logging.WARNING, logger=hl_module.__name__):
        order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.REJECTED
    assert "treating as REJECTED" in caplog.text


@pytest.mark.parametrize("total_sz", ["abc", "", "nan", "inf"])
async def test_unparseable_total_sz_falls_back_to_the_submitted_size(ex, total_sz):
    ex._exchange.order.return_value = _filled(total_sz=total_sz)

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)


def _account_with(coin: str, szi: str, entry_px: str) -> dict:
    return {
        **EMPTY_ACCOUNT,
        "assetPositions": [{"type": "oneWay", "position": {
            "coin": coin, "szi": szi, "entryPx": entry_px,
            "unrealizedPnl": "0.0", "liquidationPx": None,
        }}],
    }


def _order_answer(total_sz: str) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": total_sz, "avgPx": "50010.0", "oid": 555}},
    ]}}}


async def test_real_sdk_order_books_the_rounded_size(hl_http, build_exchange):
    """End to end through the real SDK `Exchange.order` (signing and all):
    0.002523 BTC goes on the wire as 0.00252 and comes back as that."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, EMPTY_ACCOUNT))
    hl_http.route("order", (200, _order_answer("0.00252")))

    order = await exchange.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)
    assert order.id == "555"


async def test_real_sdk_zero_total_sz_is_rejected(hl_http, build_exchange):
    """The reviewer's reproduction: `totalSz: "0.0"` through the real SDK
    used to come back FILLED at 0.00252."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, EMPTY_ACCOUNT))
    hl_http.route("order", (200, _order_answer("0.0")))

    order = await exchange.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.REJECTED


# --- 7. A malformed 200 on a read is an error, not an empty book ------------


async def test_html_200_on_user_state_raises_instead_of_reading_flat(
    hl_http, build_exchange, no_sleep,
):
    """The #167 bug through a different door: the SDK turns an HTML 200
    into `{"error": …}`, which used to read as no positions and $0."""
    from hypertrade.engine.runner import _is_transient_network_error

    exchange = build_exchange()
    hl_http.route("clearinghouseState", (200, HTML_PAGE))

    with pytest.raises(ExchangeReadError) as excinfo:
        await exchange.get_positions()
    assert isinstance(excinfo.value.__cause__, hl_module._MalformedResponseError)
    # Retried like the 502 it stands in for, and classed transient by the
    # runner, so an outage of error pages goes to the outage aggregator
    # rather than one Telegram error per strategy per tick.
    assert hl_http.count("clearinghouseState") == 3
    assert _is_transient_network_error(excinfo.value)

    with pytest.raises(ExchangeReadError):
        await exchange.get_balance()
    with pytest.raises(ExchangeReadError):
        await exchange.get_position("BTC")


async def test_html_200_on_user_state_recovers_on_retry(
    hl_http, build_exchange, no_sleep,
):
    exchange = build_exchange()
    hl_http.route(
        "clearinghouseState", (200, HTML_PAGE),
        (200, _account_with("BTC", "0.00252", "50100.0")),
    )

    positions = await exchange.get_positions()

    assert [(p.symbol, p.side) for p in positions] == [("BTC", "long")]
    assert positions[0].size == pytest.approx(0.00252, abs=1e-12)


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "Could not parse JSON: <html>"},
        {"assetPositions": []},
        {"marginSummary": {"accountValue": "900.0"}},
        {"assetPositions": {}, "marginSummary": {}},
        [],
        None,
    ],
)
async def test_user_state_without_the_promised_shape_is_a_read_error(
    ex, no_sleep, payload,
):
    ex._info.user_state.return_value = payload

    with pytest.raises(ExchangeReadError):
        await ex.get_positions()
    with pytest.raises(ExchangeReadError):
        await ex.get_balance()


async def test_a_genuinely_empty_account_is_still_flat(hl_http, build_exchange):
    exchange = build_exchange()
    hl_http.route("clearinghouseState", (200, EMPTY_ACCOUNT))

    assert await exchange.get_positions() == []
    assert (await exchange.get_balance()).total == pytest.approx(900.0)


async def test_html_200_on_all_mids_fails_closed(hl_http, build_exchange, no_sleep):
    """No price is the safe answer (`place_order` refuses a market order
    without a mid); the error page is retried first."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, HTML_PAGE))

    assert await exchange.get_current_price("BTC") == 0.0
    assert hl_http.count("allMids") == 3


@pytest.mark.parametrize(
    ("kind", "call"),
    [
        ("userFills", lambda ex: ex.fetch_user_fills()),
        ("userFillsByTime", lambda ex: ex.fetch_user_fills(since_ms=1)),
        ("userFunding", lambda ex: ex.get_user_funding_history(1)),
    ],
)
async def test_html_200_on_list_reads_returns_empty_not_a_dict(
    hl_http, build_exchange, no_sleep, kind, call,
):
    """These already turn any failure into `[]`; an error page used to
    come back as the `{"error": …}` dict itself."""
    exchange = build_exchange()
    hl_http.route(kind, (200, HTML_PAGE))

    assert await call(exchange) == []
    assert hl_http.count(kind) == 3


# --- 8. Order POST with an unknown outcome gets the delayed-fill poll -------


@pytest.mark.parametrize(
    ("exc", "unknown"),
    [
        (asyncio.TimeoutError(), True),
        (requests.exceptions.ReadTimeout("sdk socket timeout"), True),
        (requests.exceptions.ConnectionError("connection reset"), True),
        (ConnectionError("reset"), True),
        (ServerError(504, "Gateway Timeout"), True),
        (ServerError(502, "Bad Gateway"), False),
        (ServerError(503, "Service Unavailable"), False),
        (ServerError(500, "Internal Server Error"), False),
        (ClientError(422, None, "invalid size", None), False),
        (ValueError("float_to_wire causes rounding"), False),
    ],
)
def test_order_outcome_unknown_predicate(exc, unknown):
    assert _order_outcome_unknown(exc) is unknown


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ReadTimeout("sdk socket timeout"),
        requests.exceptions.ConnectionError("connection reset"),
        ServerError(504, "Gateway Timeout"),
    ],
)
async def test_unknown_outcome_polls_and_finds_the_fill(ex, no_sleep, exc):
    """The SDK's own `requests` timeout can fire before our `wait_for`
    (same deadline). It used to land in the generic handler: REJECTED,
    no poll, while HL had filled the order."""
    ex._exchange.order.side_effect = exc
    ex._signed_position_size = AsyncMock(side_effect=[0.0, 0.00252])
    ex.get_position = AsyncMock(return_value=None)

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)
    assert ex._exchange.order.call_count == 1  # polled, never resubmitted


async def test_unknown_outcome_without_a_fill_is_rejected_after_polling(
    ex, no_sleep,
):
    ex._exchange.order.side_effect = requests.exceptions.ReadTimeout("slow")
    ex._signed_position_size = AsyncMock(return_value=0.0)

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.REJECTED
    assert ex._signed_position_size.call_count > 2  # baseline + polls


@pytest.mark.parametrize(
    "exc",
    [
        ServerError(503, "Service Unavailable"),
        ClientError(422, None, "invalid size", None),
        ValueError("bug"),
    ],
)
async def test_clear_order_failure_is_rejected_without_polling(ex, no_sleep, exc):
    ex._exchange.order.side_effect = exc
    ex._signed_position_size = AsyncMock(return_value=0.0)

    order = await ex.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.REJECTED
    assert ex._signed_position_size.call_count == 1  # the baseline only


async def test_real_sdk_read_timeout_on_order_post_polls(
    hl_http, build_exchange, no_sleep,
):
    """Same, end to end: `requests` raises inside the real SDK call."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route(
        "clearinghouseState", (200, EMPTY_ACCOUNT),
        (200, _account_with("BTC", "0.00252", "50100.0")),
    )
    hl_http.route("order", requests.exceptions.ReadTimeout("read timed out"))

    order = await exchange.place_order("BTC", "buy", 0.002523)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.00252, abs=1e-12)
    assert order.filled_price == pytest.approx(50_100.0)
    assert hl_http.count("order") == 1


# --- 9. A 4xx without the SDK's error fields keeps its status (reads) -------


def test_stock_sdk_turns_a_codeless_429_into_key_error(hl_http):
    """Why `_ReadInfo` exists: the stock SDK indexes `err["code"]`."""
    from hyperliquid.utils import constants

    info = Info(constants.TESTNET_API_URL, skip_ws=True, meta=META,
                spot_meta=SPOT_META)
    hl_http.route("allMids", (429, {"message": "rate limited"}))

    with pytest.raises(KeyError):
        info.all_mids()


async def test_codeless_429_on_a_read_is_retried(hl_http, build_exchange, no_sleep):
    exchange = build_exchange()
    hl_http.route(
        "allMids", (429, {"message": "rate limited"}), (200, {"BTC": "50000.0"}),
    )

    assert await exchange.get_current_price("BTC") == pytest.approx(50_000.0)
    assert hl_http.count("allMids") == 2


async def test_codeless_400_on_a_read_is_not_retried(
    hl_http, build_exchange, no_sleep,
):
    exchange = build_exchange()
    hl_http.route("allMids", (400, {"message": "bad request"}))

    assert await exchange.get_current_price("BTC") == 0.0
    assert hl_http.count("allMids") == 1


# --- 10. The diagnostic endpoint builds the exchange off the event loop ------


async def test_diagnostic_constructs_the_exchange_in_a_worker_thread(monkeypatch):
    """Construction blocks (metadata HTTP + `time.sleep` backoff); on the
    event loop one call during an HL outage froze the API for 30-105 s."""
    import threading

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from hypertrade import api as api_module
    from hypertrade.exchange.base import Balance

    seen = {}

    class _FakeHL:
        signer_address = address = "0xabc"

        def __init__(self):
            seen["thread"] = threading.get_ident()

        async def get_balance(self):
            return Balance(total=1.0, available=1.0)

        async def get_positions(self):
            return []

        async def get_current_price(self, _symbol):
            return 50_000.0

    monkeypatch.setattr(hl_module, "HyperLiquidExchange", _FakeHL)
    monkeypatch.setattr(api_module.settings, "api_key", "")
    monkeypatch.setattr(
        api_module.settings, "hyperliquid_private_key", "0x" + "1" * 64,
    )
    app = web.Application()
    app.router.add_get("/diag", api_module.hyperliquid_diagnostic)

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async with client.get("/diag") as resp:
            body = await resp.json()
    finally:
        await client.close()

    assert body["ok"] is True
    assert seen["thread"] != threading.get_ident()
