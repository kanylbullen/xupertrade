"""Tests for HyperLiquid place_order timeout-poll (audit H2).

Pre-fix: a 15s timeout in `place_order` returned REJECTED. If HL
eventually filled the order, the exchange had a position the DB
didn't. Reconcile caught it 5 min later, but during those 5 min an
SL-driven strategy couldn't manage the position.

Post-fix: on timeout, poll the position endpoint for ~30s. If the
signed size moved by approximately the requested amount, return
FILLED with the observed entry price. Otherwise REJECTED.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.exchange.base import Order, OrderStatus, OrderType, Position
from hypertrade.exchange.hyperliquid import HyperLiquidExchange


def _exchange_stub() -> HyperLiquidExchange:
    """Build a HyperLiquidExchange skeleton without going through real
    SDK init. Tests configure get_position / _signed_position_size as
    needed."""
    ex = HyperLiquidExchange.__new__(HyperLiquidExchange)
    ex._sz_decimals = {"BTC": 5, "ETH": 4, "SOL": 2}
    return ex


@pytest.mark.asyncio
async def test_poll_detects_delayed_long_fill_and_returns_filled(monkeypatch):
    """Timeout fires; the next poll shows position grew by exactly the
    requested size — treat as FILLED at the observed entry price."""
    ex = _exchange_stub()
    # Pre-order: 0; post-fill: +0.05 BTC (matches the expected delta)
    ex.get_position = AsyncMock(return_value=Position(
        symbol="BTC", side="long", size=0.05, entry_price=50_000.0,
    ))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="buy", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET,
        price=None, pre_signed=0.0, limit_px=50_000.0,
    )
    assert order.status == OrderStatus.FILLED
    assert order.filled_price == pytest.approx(50_000.0)


@pytest.mark.asyncio
async def test_poll_detects_short_fill(monkeypatch):
    """For sells, expected delta is negative. Pre 0, post -1 SOL."""
    ex = _exchange_stub()
    ex.get_position = AsyncMock(return_value=Position(
        symbol="SOL", side="short", size=1.0, entry_price=100.0,
    ))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    order = await ex._poll_for_delayed_fill(
        symbol="SOL", side="sell", requested_size=1.0,
        requested_rounded=1.0, order_type=OrderType.MARKET,
        price=None, pre_signed=0.0, limit_px=100.0,
    )
    assert order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_poll_no_fill_returns_rejected(monkeypatch):
    """If the position size never changes during polling, the order
    really did time out — return REJECTED."""
    ex = _exchange_stub()
    # Position never appears
    ex.get_position = AsyncMock(return_value=None)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="buy", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET,
        price=None, pre_signed=0.0, limit_px=50_000.0,
    )
    assert order.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_poll_rejects_when_pre_signed_unknown(monkeypatch):
    """If the pre-order baseline read failed, we can't tell if the
    position grew because of OUR order or someone else's. Safer to
    return REJECTED than to misclassify."""
    ex = _exchange_stub()
    ex.get_position = AsyncMock(return_value=Position(
        symbol="BTC", side="long", size=0.05, entry_price=50_000.0,
    ))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="buy", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET,
        price=None, pre_signed=None, limit_px=50_000.0,
    )
    assert order.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_poll_tolerates_szDecimals_rounding(monkeypatch):
    """The signed-size match uses a tolerance equal to one min step.
    BTC has 5 dp → tolerance 1e-5. Position 0.0500001 vs target 0.05
    must still be treated as a fill."""
    ex = _exchange_stub()
    ex.get_position = AsyncMock(return_value=Position(
        symbol="BTC", side="long", size=0.0500001, entry_price=50_000.0,
    ))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="buy", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET,
        price=None, pre_signed=0.0, limit_px=50_000.0,
    )
    assert order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_poll_eventually_finds_late_fill(monkeypatch):
    """Position is empty for the first 3 polls then appears — must still
    be detected as FILLED, not REJECTED."""
    ex = _exchange_stub()
    seq = [
        Position(symbol="BTC", side="long", size=0.0, entry_price=0.0),
        Position(symbol="BTC", side="long", size=0.0, entry_price=0.0),
        Position(symbol="BTC", side="long", size=0.0, entry_price=0.0),
        Position(symbol="BTC", side="long", size=0.05, entry_price=50_000.0),
    ]
    ex.get_position = AsyncMock(side_effect=seq + [seq[-1]] * 10)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))

    order = await ex._poll_for_delayed_fill(
        symbol="BTC", side="buy", requested_size=0.05,
        requested_rounded=0.05, order_type=OrderType.MARKET,
        price=None, pre_signed=0.0, limit_px=50_000.0,
    )
    assert order.status == OrderStatus.FILLED
