"""Provider dispatch tests for `get_provider()`.

Verifies that the config-driven selector picks the right backend or
returns None when misconfigured. Doesn't make real HTTP calls.
"""

from __future__ import annotations

from unittest.mock import patch

from hypertrade.portfolio.providers import get_provider
from hypertrade.portfolio.providers.coinstats import CoinStatsProvider
from hypertrade.portfolio.providers.rotki import RotkiProvider


def _settings(**overrides):
    """Return an object with attribute access matching the fields read
    by `get_provider()`. Any field not overridden is empty/None."""
    base = {
        "portfolio_provider": "",
        "coinstats_api_key": "",
        "coinstats_share_token": "",
        "coinstats_passcode": "",
        "rotki_url": "",
        "rotki_username": "",
        "rotki_password": "",
    }
    base.update(overrides)
    return type("Cfg", (), base)()


def test_no_provider_when_unset():
    with patch("hypertrade.portfolio.providers.settings", _settings()):
        assert get_provider() is None


def test_no_provider_when_unknown_name():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(portfolio_provider="bogus"),
    ):
        assert get_provider() is None


def test_coinstats_provider_when_configured():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_provider="coinstats",
            coinstats_api_key="k",
            coinstats_share_token="t",
        ),
    ):
        p = get_provider()
        assert isinstance(p, CoinStatsProvider)
        assert p.name == "coinstats"


def test_coinstats_returns_none_when_creds_missing():
    """Selected `coinstats` but missing keys → None, not a half-broken
    provider that would 4xx every call."""
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(portfolio_provider="coinstats"),
    ):
        assert get_provider() is None


def test_rotki_provider_when_configured():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_provider="rotki",
            rotki_url="http://rotki:5042",
            rotki_username="alice",
            rotki_password="hunter2",
        ),
    ):
        p = get_provider()
        assert isinstance(p, RotkiProvider)
        assert p.name == "rotki"
        assert p.base_url == "http://rotki:5042"


def test_rotki_returns_none_when_url_missing():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_provider="rotki",
            rotki_username="alice",
            rotki_password="hunter2",
        ),
    ):
        assert get_provider() is None


def test_rotki_url_trailing_slash_is_stripped():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_provider="rotki",
            rotki_url="http://rotki:5042/",
            rotki_username="u",
            rotki_password="p",
        ),
    ):
        p = get_provider()
        assert isinstance(p, RotkiProvider)
        assert p.base_url == "http://rotki:5042"
