"""Reduce-only orders at the exchange layer (NU-5a).

Every close goes out reduce-only, so no size can open or flip a position.
The rule is HyperLiquid's: a reduce-only order fills at most the position
it reduces and is rejected against a flat position or one on its own side
("Reduce only order would increase position."). The HL wrapper passes the
flag to the SDK (`r` on the wire); the paper exchange emulates the rule.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

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


async def test_hl_reduce_only_refusal_is_rejected(hl_http, build_exchange, caplog):
    """REJECTED with HL's reason; logged below WARNING, because the runner
    logs each close attempt once, at the level its episode calls for."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0"}))
    hl_http.route("clearinghouseState", (200, EMPTY_ACCOUNT))
    hl_http.route("order", (200, {"status": "ok", "response": {
        "type": "order", "data": {"statuses": [
            {"error": "Reduce only order would increase position. asset=0"},
        ]},
    }}))

    with caplog.at_level(logging.INFO):
        order = await exchange.place_order("BTC", "sell", 0.01, reduce_only=True)

    assert order.status == OrderStatus.REJECTED
    assert order.error.startswith("Reduce only order would increase position")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("fault", ["no szDecimals", "size rounds to 0", "raised"])
async def test_a_refused_reduce_only_order_logs_below_warning(
    hl_http, build_exchange, caplog, fault,
):
    """The runner re-sends a refused close every tick and logs each attempt
    once; the wrapper's own lines for it stay at INFO."""
    exchange = build_exchange()
    hl_http.route("allMids", (200, {"BTC": "50000.0", "XYZ": "1.0"}))
    hl_http.route("clearinghouseState", (200, _account_with("BTC", "0.01")))
    if fault == "raised":
        exchange._exchange.order = MagicMock(side_effect=ValueError("bad signature"))
    symbol, size = {"no szDecimals": ("XYZ", 1.0), "size rounds to 0":
                    ("BTC", 1e-9), "raised": ("BTC", 0.01)}[fault]

    with caplog.at_level(logging.INFO):
        order = await exchange.place_order(symbol, "sell", size, reduce_only=True)

    assert order.status == OrderStatus.REJECTED and order.error
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


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


# --- HyperLiquid: a close whose POST timed out ------------------------------


def _stub(mid=50_000.0, signed=(0.0,)) -> HyperLiquidExchange:
    ex = HyperLiquidExchange.__new__(HyperLiquidExchange)
    ex._sz_decimals = {"BTC": 5}
    ex.get_current_price = AsyncMock(return_value=mid)
    ex.get_position = AsyncMock(return_value=None)
    ex._signed_position_size = AsyncMock(side_effect=list(signed) * 20)
    return ex


async def _poll(ex, monkeypatch, *, reduce_only=True):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))
    return await ex._poll_for_delayed_fill(
        symbol="BTC", side="sell", requested_size=0.02,
        requested_rounded=0.02, order_type=OrderType.MARKET, price=None,
        pre_signed=0.02, limit_px=47_500.0, reduce_only=reduce_only,
        send_mid=49_000.0,
    )


@pytest.mark.parametrize("mid, priced", [(50_000.0, 50_000.0), (0.0, 49_000.0)],
                         ids=["mid", "no-mid"])
async def test_a_late_close_is_priced_at_the_mid_never_its_limit(
    monkeypatch, mid, priced,
):
    """A close found filled after its POST timed out leaves no position to
    read a price from; its limit can be 5 % off on the wide band. With no
    mid now (HL degraded), the mid it was sent at."""
    order = await _poll(_stub(mid=mid), monkeypatch)

    assert order.status == OrderStatus.FILLED
    assert order.filled_price == pytest.approx(priced)


async def test_a_timeout_with_no_fill_says_the_outcome_is_unknown(monkeypatch):
    order = await _poll(_stub(signed=(0.02,)), monkeypatch)

    assert order.status == OrderStatus.REJECTED
    assert order.error.startswith("outcome unknown")


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
