"""HyperLiquid exchange-wrapper hardening (analysis-2026-09-15 § 4).

1. `Order.size` is what HL took (filled `totalSz`, else the szDecimals-
   rounded size that was submitted), never the unrounded request. The
   request was what the runner booked, which is the live 247x
   "BTC size mismatch — DB total 0.002523 vs exchange 0.002520" warning.
2. A failed or empty `meta()` at construction is retried and then fatal,
   and an order for a coin with no szDecimals is refused — no silent
   4 dp fallback.
3. Reads retry transient network errors and 408/429/502/503/504 only;
   a 4xx or a plain 500 fails on the first attempt. Writes never retry.
4. `cancel_order` calls the SDK with its real signature and only reports
   success when HL confirms it.
5. An order HL leaves resting is cancelled, not silently left to fill.

The SDK objects are `create_autospec`'d from the real classes, so a call
with the wrong arity fails here instead of in production.
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

from hypertrade.config import settings
from hypertrade.exchange import hyperliquid as hl_module
from hypertrade.exchange.base import OrderStatus, OrderType
from hypertrade.exchange.hyperliquid import (
    HyperLiquidExchange,
    _is_retryable_server_error,
)


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
    exchange._info.user_state.return_value = {"assetPositions": []}
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


def _construct(info_factory, attempts=3):
    with patch.object(hl_module, "Info", side_effect=info_factory), \
         patch.object(hl_module, "HLExchange", return_value=MagicMock()), \
         patch.object(settings, "hl_init_retry_attempts", attempts), \
         patch.object(settings, "hl_init_retry_backoff_seconds", 0.001), \
         patch.object(settings, "hyperliquid_private_key", "0x" + "1" * 64):
        return hl_module.HyperLiquidExchange()


def test_meta_fetch_is_retried_at_construction():
    """A 503 on the meta read used to be logged and ignored, leaving an
    empty szDecimals map. It is now retried inside the init loop."""
    info = MagicMock()
    info.meta.side_effect = [
        ServerError(503, "Service Unavailable"),
        {"universe": [{"name": "BTC", "szDecimals": 5},
                      {"name": "SOL", "szDecimals": 2}]},
    ]

    exchange = _construct(lambda *a, **k: info)

    assert exchange._sz_decimals == {"BTC": 5, "SOL": 2}
    assert info.meta.call_count == 2


def test_construction_fails_when_meta_stays_empty():
    """Booting with no szDecimals is how SOL orders went out at 4 dp and
    422'd while the bot looked healthy. Refuse to start instead."""
    info = MagicMock()
    info.meta.return_value = {"universe": []}

    with pytest.raises(RuntimeError, match="no perp szDecimals"):
        _construct(lambda *a, **k: info, attempts=3)
    assert info.meta.call_count == 3


def test_construction_fails_when_meta_stays_unreachable():
    info = MagicMock()
    info.meta.side_effect = ServerError(502, "Bad Gateway")

    with pytest.raises(RuntimeError, match="HyperLiquid API unreachable"):
        _construct(lambda *a, **k: info, attempts=3)
    assert info.meta.call_count == 3


def test_non_json_meta_answer_is_treated_as_empty():
    """The SDK turns a non-JSON 200 into `{"error": "Could not parse
    JSON: …"}` — no universe, so not a usable answer."""
    info = MagicMock()
    info.meta.return_value = {"error": "Could not parse JSON: <html>"}

    with pytest.raises(RuntimeError, match="no perp szDecimals"):
        _construct(lambda *a, **k: info, attempts=2)


def test_malformed_universe_entries_are_skipped():
    info = MagicMock()
    info.meta.return_value = {"universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "", "szDecimals": 3},
        {"name": "BAD", "szDecimals": "x"},
        {"name": "NOSZ"},
        "junk",
    ]}

    exchange = _construct(lambda *a, **k: info)

    assert exchange._sz_decimals == {"BTC": 5}


def test_non_transient_meta_error_is_not_retried():
    info = MagicMock()
    info.meta.side_effect = ClientError(401, None, "unauthorized", None)

    with pytest.raises(ClientError):
        _construct(lambda *a, **k: info, attempts=5)
    assert info.meta.call_count == 1


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


# --- 3. Read retry: transient + 5xx gateway only; writes never -------------


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (ServerError(502, "Bad Gateway"), True),
        (ServerError(503, "Service Unavailable"), True),
        (ServerError(504, "Gateway Timeout"), True),
        (ServerError(500, "upstream said 502 earlier"), False),
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
        (ValueError("bug"), False),
        (KeyError("universe"), False),
    ],
)
def test_retry_predicate(exc, retryable):
    assert _is_retryable_server_error(exc) is retryable


async def test_read_retries_a_5xx_until_it_succeeds(ex, no_sleep):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServerError(503, "Service Unavailable")
        return {"BTC": "50000.0"}

    assert await ex._run_with_retry(flaky) == {"BTC": "50000.0"}
    assert calls["n"] == 3


@pytest.mark.parametrize(
    "exc",
    [
        ServerError(400, "Bad Request"),
        ServerError(500, "Internal Server Error"),
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


async def test_order_write_is_never_retried(ex, no_sleep):
    """A retryable-looking 503 on the order POST must not be resubmitted:
    the first attempt may have reached the matching engine."""
    ex._exchange.order.side_effect = ServerError(503, "Service Unavailable")

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
