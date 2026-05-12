"""Tests for /api/internal/send-unlock-link (PR 3c).

The dashboard calls this on the bot to DM a tenant a signed
unlock URL. Tests verify API_KEY gating, body validation, no-
telegram-configured response, and the happy path delegating to
TelegramNotifier.send.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from hypertrade import api as api_module
from hypertrade.config import settings


@pytest.fixture
def app_with_unlock_route(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-api-key-1234")
    telegram = MagicMock()
    telegram._token = "fake-bot-token"
    telegram.send = AsyncMock(return_value=True)
    app = web.Application()
    app["telegram"] = telegram
    app.router.add_post(
        "/api/internal/send-unlock-link",
        api_module.send_unlock_link_handler,
    )
    return app, telegram


async def _post(app, path: str, *, body=None, headers=None):
    from aiohttp.test_utils import TestServer, TestClient
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.post(
            path, json=body or {}, headers=headers or {}
        ) as resp:
            return resp.status, await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_unlock_link_requires_api_key(app_with_unlock_route):
    app, telegram = app_with_unlock_route
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={"chat_id": "1234", "url": "https://example.com/unlock?token=x"},
    )
    assert status == 401
    telegram.send.assert_not_called()


@pytest.mark.asyncio
async def test_send_unlock_link_happy_path(app_with_unlock_route):
    app, telegram = app_with_unlock_route
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={
            "chat_id": "1234567890",
            "url": "https://example.com/unlock?token=abc",
        },
        headers={"X-Api-Key": "test-api-key-1234"},
    )
    assert status == 200
    assert body == {"sent": True}
    telegram.send.assert_awaited_once()
    call = telegram.send.await_args
    sent_text = call.args[0]
    assert "Unlock required" in sent_text
    assert "https://example.com/unlock?token=abc" in sent_text
    assert call.kwargs.get("to_chat_id") == "1234567890"


@pytest.mark.asyncio
async def test_send_unlock_link_html_escapes_url(app_with_unlock_route):
    """The DM uses parse_mode=HTML; the URL must be escaped or a
    quote/`>` in the URL would inject HTML into the message."""
    app, telegram = app_with_unlock_route
    # Synthetic URL with HTML-significant chars that the validator
    # accepts (passes startswith https://). In real life the dashboard
    # always sends plain base64url tokens, but defense-in-depth.
    bad_url = 'https://example.com/unlock?token=a"><script>x</script>'
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={"chat_id": "1234", "url": bad_url},
        headers={"X-Api-Key": "test-api-key-1234"},
    )
    assert status == 200
    sent_text = telegram.send.await_args.args[0]
    # Raw injection markers must NOT appear in the sent text.
    assert "<script>" not in sent_text
    # Escaped form (with &amp;quot;) should be present.
    assert "&quot;" in sent_text or "&lt;script&gt;" in sent_text


@pytest.mark.asyncio
async def test_send_unlock_link_rejects_non_https_url(app_with_unlock_route):
    app, telegram = app_with_unlock_route
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={"chat_id": "1234", "url": "http://insecure.example.com/x"},
        headers={"X-Api-Key": "test-api-key-1234"},
    )
    assert status == 400
    telegram.send.assert_not_called()


@pytest.mark.asyncio
async def test_send_unlock_link_rejects_missing_chat_id(app_with_unlock_route):
    app, telegram = app_with_unlock_route
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={"url": "https://example.com/unlock"},
        headers={"X-Api-Key": "test-api-key-1234"},
    )
    assert status == 400
    telegram.send.assert_not_called()


@pytest.mark.asyncio
async def test_send_unlock_link_503_when_telegram_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-api-key-1234")
    telegram = MagicMock()
    telegram._token = ""  # no token configured
    app = web.Application()
    app["telegram"] = telegram
    app.router.add_post(
        "/api/internal/send-unlock-link",
        api_module.send_unlock_link_handler,
    )
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={"chat_id": "1234", "url": "https://example.com/unlock"},
        headers={"X-Api-Key": "test-api-key-1234"},
    )
    assert status == 503


@pytest.mark.asyncio
async def test_send_unlock_link_502_when_send_fails(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-api-key-1234")
    telegram = MagicMock()
    telegram._token = "fake"
    telegram.send = AsyncMock(return_value=False)  # send returns False
    app = web.Application()
    app["telegram"] = telegram
    app.router.add_post(
        "/api/internal/send-unlock-link",
        api_module.send_unlock_link_handler,
    )
    status, body = await _post(
        app,
        "/api/internal/send-unlock-link",
        body={"chat_id": "1234", "url": "https://example.com/unlock"},
        headers={"X-Api-Key": "test-api-key-1234"},
    )
    assert status == 502
