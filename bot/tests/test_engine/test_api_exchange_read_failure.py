"""The API must not answer an unreadable exchange with a flat book.

`GET /api/positions` and `/api/control/state` returned an empty list and
a $0 equity when `get_positions()` swallowed the error. The dashboard
rendered "no open positions" while positions were live on HyperLiquid
(`bot/reports/analysis-2026-09-15.md` § 2). 503 is the honest answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from hypertrade import api as api_module
from hypertrade.exchange.base import Balance, ExchangeReadError, Position


async def _get(app, path: str):
    from aiohttp.test_utils import TestClient, TestServer

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.get(path) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = None
            return resp.status, body
    finally:
        await client.close()


def _control():
    control = MagicMock()
    control.is_paused = AsyncMock(return_value=False)
    control.get_disabled_strategies = AsyncMock(return_value=set())
    control.get_all_leverage_overrides = AsyncMock(return_value={})
    control.get_allow_multi_coin = AsyncMock(return_value=False)
    return control


def _app(exchange):
    app = web.Application()
    api_module._control_routes(
        app, control=_control(), exchange=exchange, strategies=[],
    )
    app.router.add_get("/api/positions", api_module.positions_handler)
    app["exchange"] = exchange
    return app


@pytest.mark.asyncio
async def test_positions_returns_503_on_read_failure(monkeypatch):
    monkeypatch.setattr(api_module.settings, "api_key", "")
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(
        side_effect=ExchangeReadError("get_positions failed: 502 Bad Gateway")
    )

    status, body = await _get(_app(exchange), "/api/positions")

    assert status == 503
    assert body["error"] == "exchange read failed"
    assert "positions" not in body


@pytest.mark.asyncio
async def test_positions_still_200_on_a_genuinely_empty_book(monkeypatch):
    monkeypatch.setattr(api_module.settings, "api_key", "")
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[])

    status, body = await _get(_app(exchange), "/api/positions")

    assert status == 200
    assert body["positions"] == []


@pytest.mark.asyncio
async def test_positions_200_with_positions(monkeypatch):
    monkeypatch.setattr(api_module.settings, "api_key", "")
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[
        Position(symbol="BTC", side="long", size=1.0, entry_price=100.0),
    ])

    status, body = await _get(_app(exchange), "/api/positions")

    assert status == 200
    assert body["positions"][0]["symbol"] == "BTC"


@pytest.mark.asyncio
async def test_control_state_returns_503_on_read_failure(monkeypatch):
    monkeypatch.setattr(api_module.settings, "api_key", "")
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(side_effect=ExchangeReadError("502"))
    exchange.get_balance = AsyncMock(side_effect=ExchangeReadError("502"))

    status, body = await _get(_app(exchange), "/api/control/state")

    assert status == 503
    assert body["error"] == "exchange read failed"


@pytest.mark.asyncio
async def test_control_state_ok_when_the_exchange_answers(monkeypatch):
    monkeypatch.setattr(api_module.settings, "api_key", "")
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=[])
    exchange.get_balance = AsyncMock(
        return_value=Balance(total=900.0, available=900.0)
    )

    status, body = await _get(_app(exchange), "/api/control/state")

    assert status == 200
    assert body["equity"] == pytest.approx(900.0)
    assert body["open_positions"] == 0
