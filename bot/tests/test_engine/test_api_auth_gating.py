"""Auth-gating tests for the bot's HTTP API.

Covers the constant-time `X-Api-Key` comparison and the gated GET
surface — the dashboard auth/tls config endpoints used to live here
too but were removed in PR 4c (the dashboard now reads/writes those
Redis keys directly).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from hypertrade import api as api_module


@pytest.fixture
def fake_control():
    """A BotControl stub with the methods the surviving routes touch."""
    control = MagicMock()
    return control


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

async def _get(app, path: str, headers: dict | None = None):
    from aiohttp.test_utils import TestServer, TestClient
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        async with client.get(path, headers=headers or {}) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = None
            return resp.status, body
    finally:
        await client.close()


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
    "/api/hodl/signals",
    "/api/hodl/levels",
    "/api/hodl/purchases",
    "/api/vaults/mine",
    # Vault endpoints gated 2026-05-09 (audit H2): they reveal which
    # vaults this user is monitoring + scanner state. Concrete sample
    # paths included so future regressions on the dynamic-route variants
    # are caught (a 0x… address and the snapshots subroute).
    "/api/vaults",
    "/api/vaults/0x1111111111111111111111111111111111111111",
    "/api/vaults/0x1111111111111111111111111111111111111111/snapshots",
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
# set. Locking /health would break Docker healthchecks.
@pytest.fixture
def app_with_all_routes_and_repo(fake_control):
    repo = MagicMock()
    repo.latest_qualified_vaults = AsyncMock(return_value=[])
    app = web.Application()
    app["repo"] = repo
    api_module._control_routes(
        app, control=fake_control, exchange=MagicMock(), strategies=[],
    )
    app.router.add_get("/health", api_module.health)
    return app


PUBLIC_GET_ENDPOINTS = [
    "/health",            # Docker healthcheck
]


@pytest.mark.parametrize("path", PUBLIC_GET_ENDPOINTS)
@pytest.mark.asyncio
async def test_public_get_endpoints_remain_open_with_api_key_set(
    app_with_all_routes_and_repo, path,
):
    """Regression: gating PRs must not accidentally lock down endpoints
    that have to stay reachable without auth. A future PR that adds
    `_require_auth` to /health would break Docker healthcheck."""
    with patch.object(api_module.settings, "api_key", "test-api-key-123"):
        status, _ = await _get(app_with_all_routes_and_repo, path)
        assert status == 200, (
            f"{path} returned {status}, expected 200 — gating regression?"
        )
