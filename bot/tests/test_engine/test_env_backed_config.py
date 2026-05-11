"""Env-first override for auth + TLS config (Phase 6c followup).

The Settings UI was removed (dashboard PR #66). Operator now sets
OIDC + CF token via Phase-injected env. control.get_{auth,tls}_config
must prefer env over Redis when the env value is non-empty; empty env
values must fall through to Redis for back-compat with deployments
that still have UI-written values there.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from hypertrade.engine.control import BotControl


class FakeRedis:
    """Minimal mget shim that returns canned values."""

    def __init__(self, values: list[str | None]):
        self._values = values

    async def mget(self, *_keys: str) -> list[str | None]:
        return list(self._values)


def _make_control(redis_values: list[str | None]) -> BotControl:
    c = BotControl.__new__(BotControl)
    c._redis = FakeRedis(redis_values)
    c._heartbeat_ttl = 60
    c._heartbeat_key = "test:heartbeat"
    return c


# ---- auth config -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_auth_config_env_overrides_redis():
    """When env has OIDC values set, they win over Redis."""
    c = _make_control(
        redis_values=[
            "basic",                # mode (Redis)
            "user@redis",           # basic_user
            "redishash",            # basic_hash
            "redis-session-secret", # session_secret
            "https://redis-issuer", # oidc_issuer
            "redis-client-id",
            "redis-client-secret",
            "redis-scopes",
        ]
    )
    with patch("hypertrade.config.settings") as mock_settings:
        mock_settings.auth_mode = "oidc"
        mock_settings.oidc_issuer = "https://env-issuer/"
        mock_settings.oidc_client_id = "env-client-id"
        mock_settings.oidc_client_secret = "env-secret"
        mock_settings.oidc_scopes = "openid email"
        cfg = await c.get_auth_config()

    assert cfg["mode"] == "oidc"
    assert cfg["oidc_issuer"] == "https://env-issuer/"
    assert cfg["oidc_client_id"] == "env-client-id"
    assert cfg["oidc_client_secret"] == "env-secret"
    assert cfg["oidc_scopes"] == "openid email"


@pytest.mark.asyncio
async def test_get_auth_config_falls_back_to_redis_when_env_empty():
    """Empty env values must fall through to Redis (back-compat)."""
    c = _make_control(
        redis_values=[
            "oidc",
            "",
            "",
            "redis-secret",
            "https://redis-issuer/",
            "redis-id",
            "redis-cs",
            "redis-scopes",
        ]
    )
    with patch("hypertrade.config.settings") as mock_settings:
        mock_settings.auth_mode = ""
        mock_settings.oidc_issuer = ""
        mock_settings.oidc_client_id = ""
        mock_settings.oidc_client_secret = ""
        mock_settings.oidc_scopes = ""
        cfg = await c.get_auth_config()

    assert cfg["mode"] == "oidc"
    assert cfg["oidc_issuer"] == "https://redis-issuer/"
    assert cfg["oidc_client_id"] == "redis-id"
    assert cfg["oidc_client_secret"] == "redis-cs"
    assert cfg["oidc_scopes"] == "redis-scopes"


@pytest.mark.asyncio
async def test_get_auth_config_defaults_scopes_when_both_empty():
    c = _make_control(redis_values=[None] * 8)
    with patch("hypertrade.config.settings") as mock_settings:
        mock_settings.auth_mode = ""
        mock_settings.oidc_issuer = ""
        mock_settings.oidc_client_id = ""
        mock_settings.oidc_client_secret = ""
        mock_settings.oidc_scopes = ""
        cfg = await c.get_auth_config()
    assert cfg["oidc_scopes"] == "openid profile email"
    assert cfg["mode"] == "disabled"


# ---- tls config ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tls_config_env_overrides_redis():
    c = _make_control(
        redis_values=["1", "redis-domain.com", "redis@example.com", "redis-cf-token"]
    )
    with patch("hypertrade.config.settings") as mock_settings:
        mock_settings.tls_enabled_env = "1"
        mock_settings.tls_domain = "env-domain.com"
        mock_settings.tls_email = "env@example.com"
        mock_settings.tls_cf_api_token = "env-cf-token"
        cfg = await c.get_tls_config()

    assert cfg["enabled"] is True
    assert cfg["domain"] == "env-domain.com"
    assert cfg["email"] == "env@example.com"
    assert cfg["cf_token"] == "env-cf-token"


@pytest.mark.asyncio
async def test_get_tls_config_falls_back_to_redis_when_env_empty():
    c = _make_control(
        redis_values=["1", "redis-domain.com", "redis@example.com", "redis-cf-token"]
    )
    with patch("hypertrade.config.settings") as mock_settings:
        mock_settings.tls_enabled_env = ""
        mock_settings.tls_domain = ""
        mock_settings.tls_email = ""
        mock_settings.tls_cf_api_token = ""
        cfg = await c.get_tls_config()

    assert cfg["enabled"] is True
    assert cfg["domain"] == "redis-domain.com"
    assert cfg["email"] == "redis@example.com"
    assert cfg["cf_token"] == "redis-cf-token"


@pytest.mark.asyncio
async def test_get_tls_config_env_disabled_wins_over_redis_enabled():
    """Explicit tls_enabled_env='0' must turn off TLS even when Redis says 1."""
    c = _make_control(redis_values=["1", "redis.com", "x@y.z", "tok"])
    with patch("hypertrade.config.settings") as mock_settings:
        mock_settings.tls_enabled_env = "0"
        mock_settings.tls_domain = ""
        mock_settings.tls_email = ""
        mock_settings.tls_cf_api_token = ""
        cfg = await c.get_tls_config()
    assert cfg["enabled"] is False
    # Other fields fall through to Redis since env-strings are empty
    assert cfg["domain"] == "redis.com"
