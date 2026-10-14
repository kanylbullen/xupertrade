"""Reduce-only orders at the exchange layer (NU-5a).

Every close goes out reduce-only, so no size can open or flip a position.
The rule is HyperLiquid's: a reduce-only order fills at most the position
it reduces and is rejected against a flat position or one on its own side
("Reduce only order would increase position."). The HL wrapper passes the
flag to the SDK (`r` on the wire); the paper exchange emulates the rule;
the timeout poll waits for the clamped amount, not the requested one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest
import requests
from hyperliquid.utils.error import ClientError

from hypertrade.exchange.base import OrderStatus, OrderType, Position
from hypertrade.exchange.hyperliquid import HyperLiquidExchange
from hypertrade.exchange.paper import PaperExchange

from .conftest import EMPTY_ACCOUNT


def _account_with(coin: str, szi: str) -> dict:
    return {
        **EMPTY_ACCOUNT,
        "assetPositions": [{"type": "oneWay", "position": {
            "coin": coin, "szi": szi, "entryPx": "50000.0",
            "unrealizedPnl": "0.0", "liquidationPx": None,
        }}],
    }


def _filled(total_sz: str) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": total_sz, "avgPx": "49990.0", "oid": 777}},
    ]}}}


def _wire_order(hl_http) -> dict:
    body = next(b for b in reversed(hl_http.bodies)
                if b.get("action", {}).get("type") == "order")
    return body["action"]["orders"][0]


# --- HyperLiquid: the flag reaches the wire --------------------------------


async def test_reduce_only_goes_on_the_wire(hl_http, build_exchange):
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, _account_with("BTC", "0.01")))
    hl_http.route("order", (200, _filled("0.01")))

    order = await exchange.place_order("BTC", "sell", 0.01, reduce_only=True)

    assert order.status == OrderStatus.FILLED
    wire = _wire_order(hl_http)
    assert wire["r"] is True
    assert wire["t"] == {"limit": {"tif": "Ioc"}}


async def test_an_open_is_not_reduce_only(hl_http, build_exchange):
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, EMPTY_ACCOUNT))
    hl_http.route("order", (200, _filled("0.01")))

    await exchange.place_order("BTC", "buy", 0.01)

    assert _wire_order(hl_http)["r"] is False


async def test_oversized_reduce_only_books_what_hl_filled(hl_http, build_exchange):
    """HL clamps an oversized reduce-only order to the position; `totalSz`
    says so and that is the size booked — never the request."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, _account_with("BTC", "0.004")))
    hl_http.route("order", (200, _filled("0.004")))

    order = await exchange.place_order("BTC", "sell", 0.01, reduce_only=True)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.004, abs=1e-12)


async def test_hl_reduce_only_refusal_is_rejected(hl_http, build_exchange):
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, EMPTY_ACCOUNT))
    hl_http.route("order", (200, {"status": "ok", "response": {
        "type": "order", "data": {"statuses": [
            {"error": "Reduce only order would increase position. asset=0"},
        ]},
    }}))

    order = await exchange.place_order("BTC", "sell", 0.01, reduce_only=True)

    assert order.status == OrderStatus.REJECTED
    assert order.error.startswith("Reduce only order would increase position")


async def test_slippage_sets_the_ioc_band(hl_http, build_exchange):
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, _account_with("BTC", "0.01")))
    hl_http.route("order", (200, _filled("0.01")))

    await exchange.place_order("BTC", "sell", 0.01, reduce_only=True)
    assert float(_wire_order(hl_http)["p"]) == pytest.approx(49750.0)

    await exchange.place_order(
        "BTC", "sell", 0.01, reduce_only=True, slippage=0.05,
    )
    assert float(_wire_order(hl_http)["p"]) == pytest.approx(47500.0)


# --- HyperLiquid: the timeout poll knows the clamp --------------------------


def _stub(fills=(), mid=50_000.0) -> HyperLiquidExchange:
    ex = HyperLiquidExchange.__new__(HyperLiquidExchange)
    ex._sz_decimals = {"BTC": 5}
    ex.fetch_user_fills = AsyncMock(return_value=list(fills))
    ex.get_current_price = AsyncMock(return_value=mid)
    return ex


SENT_MS = 1_790_000_000_000


def _close_fill(px: str, sz: str = "0.02", oid: int = 4242) -> dict:
    return {"coin": "BTC", "side": "A", "sz": sz, "px": px, "fee": "0.4",
            "time": SENT_MS + 3_000, "oid": oid}


async def _poll_close(ex, monkeypatch, limit_px=47_500.0):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    ex.get_position = AsyncMock(return_value=None)  # flat after the fill
    return await ex._poll_for_delayed_fill(
        symbol="BTC", side="sell", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET, price=None,
        pre_signed=0.02, limit_px=limit_px, reduce_only=True,
        sent_ms=SENT_MS,
    )


async def test_poll_waits_for_the_clamped_reduce_only_fill(monkeypatch):
    """A reduce-only sell of 0.05 against a 0.02 long can only take the
    long to flat. Waiting for -0.03 would call a real fill REJECTED."""
    order = await _poll_close(_stub([_close_fill("49990.0")]), monkeypatch)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.02)


async def test_a_late_close_is_priced_from_its_own_fill(monkeypatch):
    """A close that filled after its POST timed out leaves no position
    to read a price from. It used to book the limit — up to 5 % off on
    the wide band. HL's closing fill since the send is the price, and its
    oid the id (so reconcile never spends that fill twice)."""
    old = {**_close_fill("45000.0", oid=1), "time": SENT_MS - 60_000}
    ex = _stub([old, _close_fill("49990.0")])

    order = await _poll_close(ex, monkeypatch)

    assert order.filled_price == pytest.approx(49_990.0)
    assert order.id == "4242"


async def test_a_late_close_with_no_fill_found_is_priced_at_the_mid(monkeypatch):
    order = await _poll_close(_stub(mid=50_000.0), monkeypatch)

    assert order.status == OrderStatus.FILLED
    assert order.filled_price == pytest.approx(50_000.0), "never the limit"
    assert order.late == "mid", "an estimate, and the order says so"


async def test_a_fill_from_before_the_send_is_not_the_late_close(monkeypatch):
    """booking-1, double-close-1. A sibling strategy's close on the coin
    filled (and was booked) 2 s before this one was sent. The match used to
    take the oldest closing fill since 5 s before the send — the sibling's:
    its price, and its oid, which the UNIQUE `trades.order_id` then
    refused. Only a fill from the send on is this order's."""
    sibling = {**_close_fill("48000.0", oid=1111), "time": SENT_MS - 2_000}
    order = await _poll_close(_stub([sibling, _close_fill("49990.0")]), monkeypatch)

    assert (order.id, order.late) == ("4242", "fill")
    assert order.filled_price == pytest.approx(49_990.0)


async def test_a_fill_stamped_just_before_the_send_is_found_if_nothing_follows(
    monkeypatch,
):
    """HL's clock against ours: with no fill from the send on, one stamped
    up to 5 s before it is still this order's."""
    early = {**_close_fill("49990.0"), "time": SENT_MS - 1_000}
    order = await _poll_close(_stub([early]), monkeypatch)

    assert (order.id, order.late) == ("4242", "fill")


async def test_one_order_split_into_fills_is_one_late_close(monkeypatch):
    order = await _poll_close(_stub([
        _close_fill("49900.0", sz="0.01"), _close_fill("50100.0", sz="0.01"),
    ]), monkeypatch)

    assert (order.id, order.late) == ("4242", "fill")
    assert order.filled_price == pytest.approx(50_000.0)


async def test_two_orders_that_could_be_the_late_close_are_not_guessed(monkeypatch):
    order = await _poll_close(_stub([
        _close_fill("48000.0", oid=1111), _close_fill("49990.0", oid=4242),
    ], mid=50_000.0), monkeypatch)

    assert order.late == "mid" and order.filled_price == pytest.approx(50_000.0)
    assert order.id not in ("1111", "4242")


async def test_a_late_close_nothing_prices_is_an_unknown_outcome(monkeypatch):
    """booking-5. No fill and no mid: it used to be booked at the limit —
    up to 5 % off on the wide band — as a real fill. Unpriced, it is an
    unknown outcome the caller classifies from a fresh read."""
    order = await _poll_close(_stub(mid=0.0), monkeypatch)

    assert order.status == OrderStatus.REJECTED
    assert order.error.startswith("outcome unknown")


async def test_a_timed_out_reduce_only_close_end_to_end(
    hl_http, build_exchange, monkeypatch,
):
    """tests-real-2, through `place_order` and the real SDK: an oversized
    reduce-only sell of 1.0 against a 0.4 long; the POST times out and HL
    fills 0.4. The poll waits for the clamped 0.4 (not -0.6), and prices
    it from this order's fill after the send — not the sibling's from
    before it, which comes first."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    exchange = build_exchange()
    now = int(time.time() * 1000)
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route(
        "clearinghouseState", (200, _account_with("BTC", "0.4")),
        (200, EMPTY_ACCOUNT),
    )
    hl_http.route("order", requests.exceptions.ReadTimeout("read timed out"))
    hl_http.route("userFillsByTime", (200, [
        {"coin": "BTC", "side": "A", "sz": "0.4", "px": "48000.0",
         "fee": "1.0", "time": now - 2_000, "oid": 1111},
        {"coin": "BTC", "side": "A", "sz": "0.4", "px": "49990.0",
         "fee": "1.0", "time": now + 60_000, "oid": 4242},
    ]))

    order = await exchange.place_order("BTC", "sell", 1.0, reduce_only=True)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(0.4)
    assert (order.id, order.late) == ("4242", "fill")
    assert order.filled_price == pytest.approx(49_990.0)
    assert _wire_order(hl_http)["r"] is True


async def test_a_timeout_with_no_fill_says_the_outcome_is_unknown(monkeypatch):
    ex = _stub()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    ex._signed_position_size = AsyncMock(return_value=0.02)  # never moves

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="sell", requested_size=0.02,
        requested_rounded=0.02, order_type=OrderType.MARKET, price=None,
        pre_signed=0.02, limit_px=49_750.0, reduce_only=True,
        sent_ms=SENT_MS,
    )

    assert order.status == OrderStatus.REJECTED
    assert order.error.startswith("outcome unknown")


async def test_poll_skips_a_reduce_only_order_with_nothing_to_reduce(monkeypatch):
    ex = _stub()
    ex.get_position = AsyncMock(return_value=Position(
        symbol="BTC", side="short", size=0.05, entry_price=50_000.0,
    ))
    sleep = AsyncMock(return_value=None)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="sell", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET, price=None,
        pre_signed=0.0, limit_px=49_750.0, reduce_only=True,
    )

    assert order.status == OrderStatus.REJECTED
    sleep.assert_not_awaited()


# --- Paper: the same rule --------------------------------------------------


def _paper(position: Position | None = None) -> PaperExchange:
    ex = PaperExchange(initial_balance=10_000.0)
    ex.set_price("ETH", 2000.0)
    if position is not None:
        ex._positions["ETH"] = position
    return ex


async def test_paper_oversized_reduce_only_is_clipped_to_the_position():
    ex = _paper(Position(symbol="ETH", side="long", size=1.0, entry_price=1900.0))

    order = await ex.place_order("ETH", "sell", 3.0, reduce_only=True)

    assert order.status == OrderStatus.FILLED
    assert order.size == pytest.approx(1.0)
    assert await ex.get_position("ETH") is None, "flat, not short 2.0"


@pytest.mark.parametrize("position", [
    None,
    Position(symbol="ETH", side="short", size=1.0, entry_price=2100.0),
], ids=["flat", "same-side"])
async def test_paper_reduce_only_that_would_increase_is_rejected(position):
    ex = _paper(position)
    before = await ex.get_balance()

    order = await ex.place_order("ETH", "sell", 1.0, reduce_only=True)

    assert order.status == OrderStatus.REJECTED
    assert order.error == "Reduce only order would increase position."
    assert await ex.get_position("ETH") == position
    assert (await ex.get_balance()).total == pytest.approx(before.total)


async def test_paper_without_reduce_only_still_nets_through():
    """Opens are unchanged: HL nets an order through the position."""
    ex = _paper(Position(symbol="ETH", side="long", size=1.0, entry_price=1900.0))

    await ex.place_order("ETH", "sell", 3.0)

    pos = await ex.get_position("ETH")
    assert (pos.side, pos.size) == ("short", pytest.approx(2.0))


async def test_one_flat_read_is_not_a_filled_close(monkeypatch):
    """r1-double-close-3. A full reduce-only close timed out and never
    reached HL, and the first poll read misses the coin (a well-formed but
    empty `assetPositions`). One read at the target — flat — booked the
    close and reset its strategy with the long still open. It takes two
    reads in a row; the next finds the long, and the outcome stays unknown
    for the caller's confirmed read (`_held_after_close`)."""
    ex = _stub(mid=50_000.0)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    ex._signed_position_size = AsyncMock(side_effect=[0.0] + [0.02] * 20)

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="sell", requested_size=0.02,
        requested_rounded=0.02, order_type=OrderType.MARKET, price=None,
        pre_signed=0.02, limit_px=49_750.0, reduce_only=True,
        sent_ms=SENT_MS,
    )

    assert order.status == OrderStatus.REJECTED
    assert order.error.startswith("outcome unknown")


@pytest.mark.parametrize("why", [
    "order refused", "no mid", "mid unreadable", "no szDecimals",
    "order raises", "size rounds to 0",
])
async def test_a_refused_reduce_only_order_logs_at_info(
    hl_http, build_exchange, caplog, monkeypatch, why,
):
    """r1-tests-and-noise-5, r2 lifecycle-2. The runner logs every close it
    sends again at the level its episode calls for, HL's reason included;
    the wrapper's own line for each attempt was one more WARNING or ERROR
    a minute — a coin with no szDecimals, an order call that raised (a
    4xx), and an all_mids read that failed (its traceback at ERROR before
    the demoted "No mid price") still were. Nothing at WARNING or above for
    a reduce-only order; an open's lines keep their level."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    exchange = build_exchange()
    mids = {"no mid": (200, {}), "mid unreadable": (429, "Too Many Requests")}
    hl_http.route("allMids", mids.get(why, (200, {"BTC": "50000.0"})))
    hl_http.route("clearinghouseState", (200, _account_with("BTC", "0.01")))
    hl_http.route("order", (400, "Bad Request") if why == "order raises" else (
        200, {"status": "err", "response": "Too many requests"},
    ))
    coin = "DOGE" if why == "no szDecimals" else "BTC"
    size = 0.000001 if why == "size rounds to 0" else 0.01

    with caplog.at_level(logging.INFO, logger="hypertrade"):
        close = await exchange.place_order(coin, "sell", size, reduce_only=True)
        n = len(caplog.records)
        opened = await exchange.place_order(coin, "buy", size)

    assert close.status == opened.status == OrderStatus.REJECTED
    assert caplog.records[:n], "the close still says why, at INFO"
    assert [
        r.getMessage() for r in caplog.records[:n] if r.levelno >= logging.WARNING
    ] == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records[n:])


async def test_a_strict_fills_read_raises_instead_of_answering_empty(
    hl_http, build_exchange, monkeypatch,
):
    """r1-booking-1. `[]` on a failed read cannot be told from "no fills";
    a caller that books what no fill covers as size drift asks `strict`."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    exchange = build_exchange()
    hl_http.route("userFillsByTime", (429, "Too Many Requests"))

    assert await exchange.fetch_user_fills(since_ms=1) == []
    with pytest.raises(ClientError):
        await exchange.fetch_user_fills(since_ms=1, strict=True)
