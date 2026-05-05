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


async def _post(app, path: str, json_body: dict | None = None,
                headers: dict | None = None):
    from aiohttp.test_utils import TestServer, TestClient
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.post(
            path, json=json_body or {}, headers=headers or {},
        ) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = None
            return resp.status, body
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


# ---------------------------------------------------------------------------
# fix/gate-getters: read-only endpoints that previously responded 200 to any
# unauthenticated caller. These leak personal data (positions, vault
# entries, HODL purchases), operational state (paused, disabled strategies,
# leverage overrides), or the wallet address (hyperliquid diagnostic).
# Once API_KEY is set on the bot, they MUST 401 without the header.
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_all_routes(fake_control):
    """Wires both the closure-bound handlers (via `_control_routes`) AND
    the module-level handlers (positions, /strategies, indicator-status,
    hyperliquid/diagnostic) so we can probe the full surface."""
    app = web.Application()
    api_module._control_routes(
        app, control=fake_control, exchange=MagicMock(), strategies=[],
    )
    app.router.add_get("/api/positions", api_module.positions_handler)
    app.router.add_get("/strategies", api_module.list_strategies_handler)
    app.router.add_get("/api/indicator-status", api_module.indicator_status)
    app.router.add_get(
        "/api/hyperliquid/diagnostic", api_module.hyperliquid_diagnostic,
    )
    return app


# Endpoints that hold personal data, operational config, or wallet info.
# All MUST return 401 when API_KEY is set and no header is sent.
GATED_GET_ENDPOINTS = [
    "/api/positions",
    "/strategies",
    "/api/indicator-status",
    "/api/hyperliquid/diagnostic",
    "/api/control/state",
    "/api/control/config",
    "/api/control/heartbeat",
    "/api/tls/config",
    "/api/hodl/signals",
    "/api/hodl/levels",
    "/api/hodl/purchases",
    "/api/vaults/mine",
]


@pytest.mark.parametrize("path", GATED_GET_ENDPOINTS)
@pytest.mark.asyncio
async def test_gated_get_endpoints_require_api_key(app_with_all_routes, path):
    """Regression: each endpoint that exposes personal/operational data
    must reject unauthenticated requests when API_KEY is set."""
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, _ = await _get(app_with_all_routes, path)
        assert status == 401, f"{path} returned {status}, expected 401"


@pytest.mark.parametrize("path", GATED_GET_ENDPOINTS)
@pytest.mark.asyncio
async def test_gated_get_endpoints_reject_wrong_key(app_with_all_routes, path):
    """Wrong key → 401 across the board."""
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, _ = await _get(
            app_with_all_routes, path, headers={"X-Api-Key": "wrong"},
        )
        assert status == 401, f"{path} returned {status}, expected 401"


# Endpoints that MUST stay reachable without auth even when API_KEY is
# set. Locking any of these would break login (config + verify), Docker
# healthchecks (/health), or the public vault scanner data.
#
# Each entry: (path, method). For POSTs we send a minimal-but-valid body
# so handlers reach a normal return path, not a 4xx parse error that
# would mask actual gating regressions.
@pytest.fixture
def fake_repo_for_vaults():
    """Stub the Repository methods that the public vault endpoints reach.
    Without this, those handlers 500 on a missing repo and the assertion
    'open without auth' becomes a false-pass."""
    repo = MagicMock()
    repo.latest_qualified_vaults = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def app_with_all_routes_and_repo(fake_control, fake_repo_for_vaults):
    app = web.Application()
    app["repo"] = fake_repo_for_vaults
    api_module._control_routes(
        app, control=fake_control, exchange=MagicMock(), strategies=[],
    )
    app.router.add_get("/api/positions", api_module.positions_handler)
    app.router.add_get("/strategies", api_module.list_strategies_handler)
    app.router.add_get("/api/indicator-status", api_module.indicator_status)
    app.router.add_get(
        "/api/hyperliquid/diagnostic", api_module.hyperliquid_diagnostic,
    )
    app.router.add_get("/health", api_module.health)
    return app


PUBLIC_GET_ENDPOINTS = [
    "/api/auth/config",   # login page renders pre-session
    "/health",            # Docker healthcheck
    "/api/vaults",        # public HL data scraped by the scanner
]


@pytest.mark.parametrize("path", PUBLIC_GET_ENDPOINTS)
@pytest.mark.asyncio
async def test_public_get_endpoints_remain_open_with_api_key_set(
    app_with_all_routes_and_repo, path,
):
    """Regression: gating PRs must not accidentally lock down endpoints
    that have to stay reachable without auth. A future PR that adds
    `_require_auth` to one of these would break login, Docker health,
    or the public vault page — this test catches it before deploy."""
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, _ = await _get(app_with_all_routes_and_repo, path)
        assert status == 200, (
            f"{path} returned {status}, expected 200 — gating regression?"
        )


@pytest.mark.asyncio
async def test_auth_verify_endpoint_open_with_api_key_set(
    app_with_all_routes_and_repo, fake_control,
):
    """`/api/auth/verify` IS the auth mechanism (basic-auth handshake) —
    it has to be reachable without `X-Api-Key`, otherwise login is
    impossible. Sends a wrong-credentials POST so the handler reaches
    the normal 401 path instead of a 4xx parse error; the point is that
    we DO get the handler's own response, not the auth gate's 401."""
    fake_control.verify_basic_auth = AsyncMock(return_value=False)
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, body = await _post(
            app_with_all_routes_and_repo,
            "/api/auth/verify",
            json_body={"username": "x", "password": "y"},
        )
        # Handler's own 401 has shape {"error": "...invalid..."}; gate's
        # 401 says "Unauthorized". Distinguish so we know it wasn't gated.
        assert body is not None
        assert "Unauthorized" not in (body.get("error") or "")
