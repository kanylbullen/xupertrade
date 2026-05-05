"""Portfolio data providers.

Each provider returns the same `PortfolioSnapshot` shape (see
`hypertrade.portfolio.models`) so the dashboard renders the same UI
regardless of which backend was queried. Selection happens at config
time via `PORTFOLIO_PROVIDER`. Add a new provider by:

  1. Implementing `PortfolioProvider` in a new module
  2. Registering it in `get_provider()` below
  3. Adding any new config fields to `Settings`

Available providers:

  - **coinstats** — third-party aggregator, requires Degen plan
    subscription, 8 credits per call. Cheap to set up but ongoing
    monthly cost.
  - **rotki** — open-source self-hosted, REST API on a Rotki backend
    you run yourself. Free; one-time setup cost. Recommended.
"""

from __future__ import annotations

from hypertrade.config import settings
from hypertrade.portfolio.providers.base import PortfolioProvider


def get_provider() -> PortfolioProvider | None:
    """Return the configured provider instance, or None when none is
    configured. The endpoint layer treats None as "not configured" and
    renders the empty-state with setup instructions.
    """
    name = (settings.portfolio_provider or "").strip().lower()
    if not name:
        return None
    if name == "coinstats":
        from hypertrade.portfolio.providers.coinstats import CoinStatsProvider
        if not (settings.coinstats_api_key and settings.coinstats_share_token):
            return None
        return CoinStatsProvider(
            api_key=settings.coinstats_api_key,
            share_token=settings.coinstats_share_token,
            passcode=settings.coinstats_passcode,
        )
    if name == "rotki":
        from hypertrade.portfolio.providers.rotki import RotkiProvider
        if not (settings.rotki_url and settings.rotki_username
                and settings.rotki_password):
            return None
        return RotkiProvider(
            base_url=settings.rotki_url,
            username=settings.rotki_username,
            password=settings.rotki_password,
        )
    return None
