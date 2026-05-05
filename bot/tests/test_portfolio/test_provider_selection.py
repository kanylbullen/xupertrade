"""Provider dispatch tests for `get_providers()`.

Verifies that the CSV-driven selector picks the right backends and
silently drops any with missing creds. No real HTTP calls.
"""

from __future__ import annotations

from unittest.mock import patch

from hypertrade.portfolio.providers import get_providers
from hypertrade.portfolio.providers.coinstats import CoinStatsProvider
from hypertrade.portfolio.providers.ghostfolio import GhostfolioProvider
from hypertrade.portfolio.providers.rotki import RotkiProvider


def _settings(**overrides):
    """Return an object with attribute access matching the fields read
    by `get_providers()`. Any field not overridden is empty/None."""
    base = {
        "portfolio_providers": "",
        "coinstats_api_key": "",
        "coinstats_share_token": "",
        "coinstats_passcode": "",
        "rotki_url": "",
        "rotki_username": "",
        "rotki_password": "",
        "ghostfolio_url": "",
        "ghostfolio_token": "",
    }
    base.update(overrides)
    return type("Cfg", (), base)()


def test_no_providers_when_unset():
    with patch("hypertrade.portfolio.providers.settings", _settings()):
        assert get_providers() == []


def test_unknown_names_silently_dropped():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(portfolio_providers="bogus,alsobogus"),
    ):
        assert get_providers() == []


def test_single_provider():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_providers="rotki",
            rotki_url="http://r:5042",
            rotki_username="u",
            rotki_password="p",
        ),
    ):
        ps = get_providers()
        assert [type(p) for p in ps] == [RotkiProvider]


def test_multiple_providers_in_listed_order():
    """The CSV order is preserved so the dashboard can render
    deterministically."""
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_providers="ghostfolio,rotki",
            rotki_url="http://r:5042",
            rotki_username="u",
            rotki_password="p",
            ghostfolio_url="http://g:3333",
            ghostfolio_token="tok",
        ),
    ):
        ps = get_providers()
        assert [p.name for p in ps] == ["ghostfolio", "rotki"]


def test_provider_silently_skipped_when_creds_missing():
    """`coinstats` listed but no api_key → it just doesn't show.
    Other providers still load."""
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_providers="rotki,coinstats",
            rotki_url="http://r:5042",
            rotki_username="u",
            rotki_password="p",
            # no coinstats_api_key
        ),
    ):
        ps = get_providers()
        assert [p.name for p in ps] == ["rotki"]


def test_star_enables_all_configured():
    """`PORTFOLIO_PROVIDERS=*` means all providers whose creds are set.
    Order follows _KNOWN_PROVIDERS dict insertion (currently rotki,
    ghostfolio, coinstats)."""
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_providers="*",
            rotki_url="http://r:5042",
            rotki_username="u",
            rotki_password="p",
            ghostfolio_url="http://g:3333",
            ghostfolio_token="tok",
            # coinstats not configured → still skipped under "*"
        ),
    ):
        ps = get_providers()
        names = [p.name for p in ps]
        assert "rotki" in names and "ghostfolio" in names
        assert "coinstats" not in names


def test_rotki_url_trailing_slash_stripped():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_providers="rotki",
            rotki_url="http://r:5042/",
            rotki_username="u",
            rotki_password="p",
        ),
    ):
        p = get_providers()[0]
        assert isinstance(p, RotkiProvider)
        assert p.base_url == "http://r:5042"


def test_ghostfolio_url_trailing_slash_stripped():
    with patch(
        "hypertrade.portfolio.providers.settings",
        _settings(
            portfolio_providers="ghostfolio",
            ghostfolio_url="http://g:3333/",
            ghostfolio_token="tok",
        ),
    ):
        p = get_providers()[0]
        assert isinstance(p, GhostfolioProvider)
        assert p.base_url == "http://g:3333"
