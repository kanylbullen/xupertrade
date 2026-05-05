"""Auth-gating tests for the bot's HTTP API.

Regression tests for the security fix that moved the dashboard's
session-signing secret off the public `/api/auth/config` endpoint and
behind an API_KEY-gated endpoint. Also covers the constant-time
`X-Api-Key` comparison.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from hypertrade import api as api_module


@pytest.fixture
def fake_control():
    """A BotControl stub that returns plausible auth-config values."""
    control = MagicMock()
    control.get_auth_config = AsyncMock(return_value={
        "mode": "basic",
        "basic_user": "alice",
        "basic_hash": "$2b$12$dummy",
        "session_secret": "super-secret-hmac-key",
        "oidc_issuer": "",
        "oidc_client_id": "",
        "oidc_client_secret": "",
        "oidc_scopes": "openid profile email",
    })
    control.ensure_session_secret = AsyncMock(
        return_value="super-secret-hmac-key"
    )
    return control


@pytest.fixture
def app_with_routes(fake_control):
    """Build an aiohttp app with the routes wired against `fake_control`."""
    app = web.Application()
    api_module._control_routes(
        app, control=fake_control, exchange=MagicMock(), strategies=[],
    )
    return app


async def _get(app, path: str, headers: dict | None = None):
    """Minimal aiohttp test harness — no real socket; uses TestClient."""
    from aiohttp.test_utils import TestServer, TestClient
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.get(path, headers=headers or {}) as resp:
            return resp.status, await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_public_auth_config_does_not_leak_session_secret(app_with_routes):
    """Regression: the public auth-config endpoint MUST NOT include
    session_secret. Anyone reachable to bot port 8001 could grab it
    and forge dashboard sessions otherwise."""
    status, body = await _get(app_with_routes, "/api/auth/config")
    assert status == 200
    assert "session_secret" not in body
    # Public fields should still be present
    assert body["mode"] == "basic"
    assert body["basic_user_set"] is True
    assert body["oidc_issuer"] == ""


@pytest.mark.asyncio
async def test_session_secret_endpoint_requires_auth_when_api_key_set(
    app_with_routes,
):
    """With API_KEY configured, requesting /session-secret without the
    header returns 401 — not the secret."""
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, body = await _get(
            app_with_routes, "/api/auth/session-secret",
        )
        assert status == 401
        assert "session_secret" not in body


@pytest.mark.asyncio
async def test_session_secret_endpoint_returns_secret_with_correct_key(
    app_with_routes,
):
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, body = await _get(
            app_with_routes,
            "/api/auth/session-secret",
            headers={"X-Api-Key": "test-api-key-123"},
        )
        assert status == 200
        assert body["session_secret"] == "super-secret-hmac-key"


@pytest.mark.asyncio
async def test_session_secret_endpoint_open_when_api_key_disabled(
    app_with_routes,
):
    """When API_KEY isn't set on the bot (e.g. local dev), every endpoint
    is open — same convention as everywhere else in the codebase."""
    with patch.object(api_module.settings, "api_key", ""):
        status, body = await _get(
            app_with_routes, "/api/auth/session-secret",
        )
        assert status == 200
        assert body["session_secret"] == "super-secret-hmac-key"


@pytest.mark.asyncio
async def test_session_secret_endpoint_rejects_wrong_key(app_with_routes):
    """Wrong key → 401. Constant-time comparison so the timing doesn't
    leak how many leading characters matched."""
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, body = await _get(
            app_with_routes,
            "/api/auth/session-secret",
            headers={"X-Api-Key": "wrong-key"},
        )
        assert status == 401
        assert "session_secret" not in body


def test_require_auth_uses_constant_time_compare():
    """The `_require_auth` helper compares using `hmac.compare_digest`,
    not `==`, so an attacker can't time-side-channel the API key
    one character at a time."""
    import inspect
    src = inspect.getsource(api_module._require_auth)
    assert "compare_digest" in src
    # And NOT the naive ==:
    assert "provided != settings.api_key" not in src
