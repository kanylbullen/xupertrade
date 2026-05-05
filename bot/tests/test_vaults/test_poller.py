"""Poller behaviour with mocked API + repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from hypertrade.vaults.models import (
    NavPoint,
    VaultDetails,
    VaultSummary,
)
from hypertrade.vaults.poller import VaultPoller


class FakeRepo:
    def __init__(self) -> None:
        self.latest: dict[str, object] = {}
        self.snapshots: list[dict] = []
        self.upserts: list[dict] = []
        self.nav_appends: list[tuple[str, int]] = []

    async def latest_vault_snapshot(self, address: str):
        return self.latest.get(address)

    async def upsert_vault(self, **kwargs):
        self.upserts.append(kwargs)

    async def append_nav_history(self, address, points):
        self.nav_appends.append((address, len(points)))
        return len(points)

    async def save_vault_snapshot(self, **kwargs):
        self.snapshots.append(kwargs)
        # Stash a tiny shim so the next poll sees previous state.
        self.latest[kwargs["vault_address"]] = type(
            "Snap", (), {"qualified": kwargs["qualified"]}
        )()
        return len(self.snapshots)


class CapturingBus:
    def __init__(self) -> None:
        self.published = []

    async def publish(self, event) -> None:
        self.published.append(event)


def _summary(address: str, **overrides) -> VaultSummary:
    base = dict(
        address=address,
        name="Vault " + address[-4:],
        leader_address="0x" + "00" * 20,
        tvl_usd=1_000_000.0,
        is_closed=False,
        relationship_type="normal",
        # 200 days of synthetic NAV; keep age < 365 so ROI 365d is waived.
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=200),
        apr=1.0,
    )
    base.update(overrides)
    return VaultSummary(**base)


def _details(address: str, sharpe_friendly: bool = True) -> VaultDetails:
    end = datetime.now(tz=timezone.utc)
    if sharpe_friendly:
        # 200 daily uptrend points → Sharpe well above 1.5, low DD.
        navs = [100.0 + i * 0.3 for i in range(200)]
    else:
        # Same average but very volatile → Sharpe < 1.5.
        navs = [100.0 + (5.0 if i % 2 else -5.0) for i in range(200)]
    nav_history = [
        NavPoint(timestamp=end - timedelta(days=200 - i), nav=v)
        for i, v in enumerate(navs)
    ]
    return VaultDetails(
        address=address,
        name="Vault " + address[-4:],
        leader_address="0x" + "00" * 20,
        description="",
        apr=1.0,
        leader_fraction=0.10,
        leader_commission=0.10,
        allow_deposits=True,
        is_closed=False,
        relationship_type="normal",
        follower_count=42,
        nav_history=nav_history,
    )


@pytest.mark.asyncio
async def test_poller_emits_qualified_then_silent_within_debounce():
    repo = FakeRepo()
    bus = CapturingBus()
    poller = VaultPoller(repo=repo, event_bus=bus, debounce_seconds=86400.0)

    summaries = [_summary("0x" + "11" * 20)]
    details = {summaries[0].address: _details(summaries[0].address)}

    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=summaries)),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value=details)),
    ):
        result = await poller.poll()
        assert result["qualified"] == 1
        assert result["newly_qualified"] == 1
        assert any(e.type == "vault.qualified" for e in bus.published)

        bus.published.clear()
        # Second poll within debounce → no new event for the same state.
        result2 = await poller.poll()
        assert result2["qualified"] == 1
        assert result2["newly_qualified"] == 0
        assert bus.published == []


@pytest.mark.asyncio
async def test_poller_disqualifies_on_state_flip():
    repo = FakeRepo()
    bus = CapturingBus()
    poller = VaultPoller(repo=repo, event_bus=bus, debounce_seconds=86400.0)

    addr = "0x" + "22" * 20
    summary = _summary(addr)
    good = _details(addr, sharpe_friendly=True)
    bad = _details(addr, sharpe_friendly=False)

    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[summary])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={addr: good})),
    ):
        await poller.poll()
        assert any(e.type == "vault.qualified" for e in bus.published)

    bus.published.clear()
    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[summary])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={addr: bad})),
    ):
        result = await poller.poll()
        assert result["newly_disqualified"] == 1
        assert any(e.type == "vault.disqualified" for e in bus.published)


@pytest.mark.asyncio
async def test_poller_handles_empty_candidate_set():
    repo = FakeRepo()
    bus = CapturingBus()
    poller = VaultPoller(repo=repo, event_bus=bus)

    closed = _summary("0x" + "33" * 20, is_closed=True)
    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[closed])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
    ):
        result = await poller.poll()
        assert result["candidates"] == 0
        assert result["qualified"] == 0
        assert bus.published == []
