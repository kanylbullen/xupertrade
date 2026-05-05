"""Portfolio data providers.

Each provider returns the same `PortfolioSnapshot` shape (see
`hypertrade.portfolio.models`) so the dashboard renders the same UI
regardless of which backend was queried. The `/portfolio` page can
display ANY NUMBER of providers side-by-side — they answer different
questions (Rotki = crypto + DeFi deep, Ghostfolio = broader asset
universe, CoinStats = SaaS quick-start). Each shows up as its own card.

Selection is via `PORTFOLIO_PROVIDERS` (comma-separated) at config
time. Special value `*` enables every provider whose connection vars
are set.

Available providers:

  - **rotki** — open-source self-hosted, AGPL. Run rotki backend
    locally; we hit its REST API. Free.
  - **ghostfolio** — open-source self-hosted, AGPL. Stocks + crypto
    + ETFs + funds. Bearer-token auth.
  - **coinstats** — third-party SaaS. Requires Degen plan, 8 credits
    per call.

Add a new provider by:
  1. Implementing `PortfolioProvider` in a new module
  2. Registering it in `_KNOWN_PROVIDERS` below
  3. Adding any new config fields to `Settings`
"""

from __future__ import annotations

from collections.abc import Callable

from hypertrade.config import settings
from hypertrade.portfolio.providers.base import PortfolioProvider


def _build_coinstats() -> PortfolioProvider | None:
    if not (settings.coinstats_api_key and settings.coinstats_share_token):
        return None
    from hypertrade.portfolio.providers.coinstats import CoinStatsProvider
    return CoinStatsProvider(
        api_key=settings.coinstats_api_key,
        share_token=settings.coinstats_share_token,
        passcode=settings.coinstats_passcode,
    )


def _build_rotki() -> PortfolioProvider | None:
    if not (settings.rotki_url and settings.rotki_username
            and settings.rotki_password):
        return None
    from hypertrade.portfolio.providers.rotki import RotkiProvider
    return RotkiProvider(
        base_url=settings.rotki_url,
        username=settings.rotki_username,
        password=settings.rotki_password,
    )


def _build_ghostfolio() -> PortfolioProvider | None:
    if not (settings.ghostfolio_url and settings.ghostfolio_token):
        return None
    from hypertrade.portfolio.providers.ghostfolio import GhostfolioProvider
    return GhostfolioProvider(
        base_url=settings.ghostfolio_url,
        token=settings.ghostfolio_token,
    )


_KNOWN_PROVIDERS: dict[str, Callable[[], PortfolioProvider | None]] = {
    "rotki": _build_rotki,
    "ghostfolio": _build_ghostfolio,
    "coinstats": _build_coinstats,
}


def get_providers() -> list[PortfolioProvider]:
    """Return all enabled providers in the order specified by
    `PORTFOLIO_PROVIDERS`. Providers whose connection vars aren't set
    are silently skipped — that way a half-configured provider doesn't
    surface as broken HTTP calls.
    """
    raw = (settings.portfolio_providers or "").strip()
    if not raw:
        return []

    if raw == "*":
        names = list(_KNOWN_PROVIDERS.keys())
    else:
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]

    out: list[PortfolioProvider] = []
    for name in names:
        builder = _KNOWN_PROVIDERS.get(name)
        if builder is None:
            continue
        provider = builder()
        if provider is not None:
            out.append(provider)
    return out
