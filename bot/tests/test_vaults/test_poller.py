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
        self.nav_store: dict[str, list] = {}
        # Vaults that should appear in `latest_qualified_vaults` — driven by
        # the most recent save_vault_snapshot with qualified=True.
        self._qualified_state: dict[str, dict] = {}
        # User vault entries: {(user, vault) -> dict}
        self.user_entries: dict[tuple[str, str], dict] = {}
        self._known_vaults: set[str] = set()

    async def latest_vault_snapshot(self, address: str):
        return self.latest.get(address)

    async def upsert_vault(self, **kwargs):
        self.upserts.append(kwargs)
        self._known_vaults.add(kwargs["address"])

    async def get_vault(self, address):
        if address in self._known_vaults:
            return type("V", (), {"address": address, "name": "v" + address[-4:]})()
        return None

    async def upsert_user_vault_entry(
        self,
        user_address,
        vault_address,
        vault_equity_usd,
        unrealized_pnl_usd,
        all_time_pnl_usd,
        days_following,
        entered_at,
        locked_until,
    ):
        key = (user_address, vault_address)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        existing = self.user_entries.get(key)
        if existing is None:
            existing = {
                "user_address": user_address,
                "vault_address": vault_address,
                "exited_at": None,
            }
            self.user_entries[key] = existing
        elif existing.get("exited_at") is not None and vault_equity_usd > 1.0:
            existing["exited_at"] = None
        existing.update({
            "vault_equity_usd": vault_equity_usd,
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "all_time_pnl_usd": all_time_pnl_usd,
            "days_following": days_following,
            "entered_at": entered_at,
            "last_seen_at": now,
            "locked_until": locked_until,
        })
        if vault_equity_usd < 1.0 and existing.get("exited_at") is None:
            existing["exited_at"] = now

    async def append_nav_history(self, address, points):
        self.nav_appends.append((address, len(points)))
        # Persist into the in-memory store so vault_nav_for() returns them.
        existing = {p.timestamp: (p.nav, p.pnl_cum) for p in self.nav_store.get(address, [])}
        for point in points:
            if len(point) == 2:
                ts, nav = point
                pnl_cum = 0.0
            else:
                ts, nav, pnl_cum = point
            existing[ts] = (nav, pnl_cum)
        from hypertrade.vaults.models import NavPoint
        self.nav_store[address] = sorted(
            (NavPoint(timestamp=ts, nav=nav, pnl_cum=pc)
             for ts, (nav, pc) in existing.items()),
            key=lambda p: p.timestamp,
        )
        return len(points)

    async def vault_nav_for(self, address):
        return self.nav_store.get(address, [])

    async def save_vault_snapshot(self, **kwargs):
        self.snapshots.append(kwargs)
        # Stash a tiny shim so the next poll sees previous state.
        addr = kwargs["vault_address"]
        snap = type(
            "Snap",
            (),
            {
                **{k: v for k, v in kwargs.items() if k != "vault_address"},
                "qualified": kwargs["qualified"],
            },
        )()
        self.latest[addr] = snap
        if kwargs["qualified"]:
            self._qualified_state[addr] = kwargs
        else:
            self._qualified_state.pop(addr, None)
        return len(self.snapshots)

    async def latest_qualified_vaults(self, *, max_age_days: int = 7):
        out = []
        for addr, kwargs in self._qualified_state.items():
            vault = type("V", (), {"address": addr, "name": "Vault " + addr[-4:]})()
            snap = type(
                "Snap",
                (),
                {**{k: v for k, v in kwargs.items() if k != "vault_address"},
                 "qualified": True},
            )()
            out.append((vault, snap))
        return out


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
async def test_poller_disqualifies_when_vault_drops_from_coarse_set():
    """A vault that previously qualified but no longer survives the coarse
    pre-filter (e.g. closed, AUM fell out of band, dropped from catalogue)
    must still get a disqualified snapshot + event — Copilot caught that the
    original implementation silently kept it in /vaults until aged out."""
    repo = FakeRepo()
    bus = CapturingBus()
    poller = VaultPoller(repo=repo, event_bus=bus, debounce_seconds=86400.0)

    addr = "0x" + "44" * 20
    summary = _summary(addr)
    good = _details(addr, sharpe_friendly=True)

    # Day 1: vault qualifies and is recorded.
    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[summary])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={addr: good})),
    ):
        await poller.poll()
    assert any(e.type == "vault.qualified" for e in bus.published)

    # Day 2: vault disappears from catalogue entirely (e.g. closed).
    bus.published.clear()
    poller._last_alert.clear()  # otherwise debounce silences day 2
    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
    ):
        result = await poller.poll()
    assert any(e.type == "vault.disqualified" for e in bus.published)
    # Confirm a disqualified row was actually persisted (so /vaults stops
    # showing it).
    assert any(
        s["vault_address"] == addr and s["qualified"] is False
        for s in repo.snapshots
    )


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


def _follower(vault: str, user: str, equity: float) -> "FollowerState":
    """Synthesize a FollowerState for a held vault."""
    from hypertrade.vaults.models import FollowerState
    return FollowerState(
        user_address=user,
        vault_address=vault,
        vault_equity_usd=equity,
        unrealized_pnl_usd=equity * 0.1,
        all_time_pnl_usd=equity * 0.4,
        days_following=120,
        entered_at=datetime.now(tz=timezone.utc) - timedelta(days=120),
        locked_until=None,
    )


@pytest.mark.asyncio
async def test_user_position_tracking_records_held_vaults_with_followerstate():
    """Each held vault gets a vaultDetails(user=...) call, and the rich
    follower state (current equity + lifetime P&L) is upserted."""
    repo = FakeRepo()
    bus = CapturingBus()
    user_addr = "0x" + "aa" * 20
    held_vault = "0x" + "55" * 20
    poller = VaultPoller(
        repo=repo, event_bus=bus, track_user_address=user_addr,
    )
    held_details = _details(held_vault)
    follower = _follower(held_vault, user_addr, equity=150.42)

    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
        patch("hypertrade.vaults.poller.fetch_user_vault_equities",
              new=AsyncMock(return_value=[{"vaultAddress": held_vault}])),
        patch("hypertrade.vaults.poller.fetch_user_vault_state",
              new=AsyncMock(return_value=(held_details, follower))),
    ):
        result = await poller.poll()

    assert result["user_positions"] == 1
    key = (user_addr, held_vault)
    assert key in repo.user_entries
    e = repo.user_entries[key]
    assert e["vault_equity_usd"] == 150.42
    assert e["all_time_pnl_usd"] == 60.168  # 150.42 * 0.4
    assert e["unrealized_pnl_usd"] == 15.042  # 150.42 * 0.1
    assert e["days_following"] == 120


@pytest.mark.asyncio
async def test_user_position_scoring_for_off_catalog_vault():
    """Held vault that didn't pass the coarse pre-filter still gets a
    snapshot row written with a verdict — that's the whole reason the
    user-position scorer exists."""
    repo = FakeRepo()
    bus = CapturingBus()
    user_addr = "0x" + "ab" * 20
    held_vault = "0x" + "77" * 20
    poller = VaultPoller(
        repo=repo, event_bus=bus, track_user_address=user_addr,
    )
    # Vault is NOT in the catalog (catalog=[]), so it definitely won't
    # land in the candidate set. The scorer should still write a snapshot.
    held_details = _details(held_vault)
    follower = _follower(held_vault, user_addr, equity=80.0)

    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
        patch("hypertrade.vaults.poller.fetch_user_vault_equities",
              new=AsyncMock(return_value=[{"vaultAddress": held_vault}])),
        patch("hypertrade.vaults.poller.fetch_user_vault_state",
              new=AsyncMock(return_value=(held_details, follower))),
    ):
        await poller.poll()

    # A snapshot row was written for this vault even though it wasn't a
    # candidate — that's what eliminates the "?" verdict in the dashboard.
    snap_addrs = [s["vault_address"] for s in repo.snapshots]
    assert held_vault in snap_addrs


@pytest.mark.asyncio
async def test_user_position_tracking_disabled_without_address():
    """No tracking address → no equities call, no DB writes."""
    repo = FakeRepo()
    bus = CapturingBus()
    poller = VaultPoller(repo=repo, event_bus=bus, track_user_address="")
    eq_mock = AsyncMock(return_value=[])
    state_mock = AsyncMock()

    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
        patch("hypertrade.vaults.poller.fetch_user_vault_equities", new=eq_mock),
        patch("hypertrade.vaults.poller.fetch_user_vault_state", new=state_mock),
    ):
        result = await poller.poll()

    assert result["user_positions"] == 0
    assert repo.user_entries == {}
    eq_mock.assert_not_called()
    state_mock.assert_not_called()


@pytest.mark.asyncio
async def test_user_position_tracking_marks_exit_on_dust():
    """Equity dropping under $1 (full withdrawal) marks the entry exited."""
    repo = FakeRepo()
    bus = CapturingBus()
    user_addr = "0x" + "bb" * 20
    vault = "0x" + "66" * 20
    poller = VaultPoller(
        repo=repo, event_bus=bus, track_user_address=user_addr,
    )

    # Day 1: deposit $200
    follower_full = _follower(vault, user_addr, equity=200.0)
    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
        patch("hypertrade.vaults.poller.fetch_user_vault_equities",
              new=AsyncMock(return_value=[{"vaultAddress": vault}])),
        patch("hypertrade.vaults.poller.fetch_user_vault_state",
              new=AsyncMock(return_value=(_details(vault), follower_full))),
    ):
        await poller.poll()

    assert repo.user_entries[(user_addr, vault)]["exited_at"] is None

    # Day 2: withdrew everything → equity dust
    follower_empty = _follower(vault, user_addr, equity=0.04)
    with (
        patch("hypertrade.vaults.poller.fetch_catalog", new=AsyncMock(return_value=[])),
        patch("hypertrade.vaults.poller.fetch_details_batch", new=AsyncMock(return_value={})),
        patch("hypertrade.vaults.poller.fetch_user_vault_equities",
              new=AsyncMock(return_value=[{"vaultAddress": vault}])),
        patch("hypertrade.vaults.poller.fetch_user_vault_state",
              new=AsyncMock(return_value=(_details(vault), follower_empty))),
    ):
        await poller.poll()

    assert repo.user_entries[(user_addr, vault)]["exited_at"] is not None
