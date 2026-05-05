"""Provider abstract base.

Each provider implements `fetch_snapshot()` and returns a
`PortfolioSnapshot`. Failures should NOT raise — return an empty
snapshot with a populated `fetched_at` so the dashboard renders the
empty-state cleanly. Log the underlying error.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hypertrade.portfolio.models import PortfolioSnapshot


class PortfolioProvider(ABC):
    """Provider name shown in logs / dashboard. Override in subclass."""

    name: str = "unknown"

    @abstractmethod
    async def fetch_snapshot(self) -> PortfolioSnapshot:
        """Return a fresh snapshot of the user's holdings."""
        ...
