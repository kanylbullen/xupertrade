"""NU-2 restore guards, runner side (roadmap `docs/plans/next-level-roadmap.md`).

Guard 1: an empty Redis reads as "not paused, nothing disabled, no kill
switch". A missing sentinel means that state was lost, so the bot turns
the kill switch on (opens blocked, exits keep running — never pause,
operator decision 5.4), sets the reconcile hold and alerts once.

Guard 2's runner half: the hold, a failed Redis read and the in-memory
sighting count decide what pass 2 may close.

Real `Repository` on SQLite, real `BotControl` on a dict-backed Redis.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hypertrade import api as api_module
from hypertrade.config import settings
from hypertrade.db.repo import ORPHAN_CONFIRM_SECONDS, Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import HOLD_REALERT_SECONDS, EngineRunner
from hypertrade.events.types import ErrorOccurred
from hypertrade.exchange.base import (
    Balance,
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)

TENANT = "00000000-0000-4000-8000-000000000001"
PREFIX = f"hypertrade:testnet:t:{TENANT}:control:"
SENTINEL = PREFIX + "sentinel"
HOLD = PREFIX + "reconcile_hold"
KILL = "hypertrade:testnet:control:kill_switch"
PAUSED = "hypertrade:testnet:control:paused"
ETH = Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)


class FakeRedis:
    """The Redis calls BotControl makes on these paths; `fail_get` and
    `fail_set` make reads or writes of a key raise."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.fail_get: set[str] = set()
        self.fail_set: set[str] = set()

    async def get(self, key):
        if key in self.fail_get:
            raise ConnectionError(f"GET {key} failed")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        if key in self.fail_set:
            raise ConnectionError(f"SET {key} failed")
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def smembers(self, key):
        return set()

    async def hgetall(self, key):
        return {}


class FakeExchange:
    def __init__(self, positions=()):
        self.positions = list(positions)
        self.read_error: Exception | None = None
        self.orders: list[tuple] = []

    async def get_positions(self):
        if self.read_error is not None:
            raise self.read_error
        return list(self.positions)

    async def get_balance(self):
        return Balance(total=1000.0, available=1000.0)

    async def fetch_user_fills(self, address=None, since_ms=None):
        return []

    async def get_current_price(self, symbol):
        return 2100.0

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET):
        self.orders.append((symbol, side, size))
        return Order(
            id=f"order-{len(self.orders)}", symbol=symbol, side=side,
            size=size, order_type=order_type, filled_price=2100.0,
            status=OrderStatus.FILLED,
        )


class Bus:
    def __init__(self):
        self.events: list = []

    async def publish(self, event):
        self.events.append(event)
        return True

    def errors(self, strategy: str) -> list[str]:
        return [
            e.message for e in self.events
            if isinstance(e, ErrorOccurred) and e.strategy == strategy
        ]


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


def _control(redis: FakeRedis, tenant: str | None = TENANT) -> BotControl:
    c = BotControl(redis_url="redis://unused/0", mode="testnet", tenant_id=tenant)
    c._redis = redis
    return c


def _runner(repo, redis, exchange, *, symbols=("ETH",)):
    bus = Bus()
    runner = EngineRunner(
        exchange=exchange,
        strategies=[SimpleNamespace(name=f"s_{s}", symbol=s) for s in symbols],
        repo=repo, event_bus=bus, control=_control(redis),
    )
    runner._restore_read_backoff = ()
    return runner, bus


async def _row(repo, symbol="BTC"):
    await repo.open_position(
        strategy_name="kalman_breakout", symbol=symbol, side="long",
        size=0.5, entry_price=60000.0,
    )


# --- guard 1 -----------------------------------------------------------


async def test_missing_sentinel_at_boot_kill_switch_hold_one_alert(repo, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    redis = FakeRedis()  # a flushed / freshly created Redis
    await _row(repo)
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    await runner.startup()

    assert redis.data[KILL] == "1"
    assert HOLD in redis.data
    assert SENTINEL in redis.data
    assert PAUSED not in redis.data  # never pause: exits must keep running
    alerts = bus.errors("restore-guard")
    assert len(alerts) == 1
    assert "kalman_breakout long 0.5 BTC" in alerts[0]
    assert "/api/control/reconcile-hold" in alerts[0]
    assert "NOT paused" in alerts[0]
    # The startup pass held the untracked ETH position instead of closing it.
    assert ex.orders == []
    assert any("HELD exchange-orphan" in m for m in bus.errors("reconcile"))
    # Opens blocked, closes allowed.
    assert await runner.portfolio.check_risk_limits(is_open=True) is False
    assert await runner.portfolio.check_risk_limits(is_open=False) is True

    # The next pass finds the sentinel: no second alert.
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("restore-guard")) == 1
    assert ex.orders == []


async def test_present_sentinel_changes_nothing(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    runner, bus = _runner(repo, redis, FakeExchange())

    await runner.startup()

    assert KILL not in redis.data
    assert HOLD not in redis.data
    assert bus.errors("restore-guard") == []


async def test_redis_lost_mid_run_is_caught_on_the_next_pass(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)
    await runner.startup()
    assert bus.errors("restore-guard") == []

    redis.data.clear()  # FLUSHALL, or redis recreated on an empty volume
    await runner._run_reconcile("Periodic")

    assert redis.data[KILL] == "1"
    assert HOLD in redis.data
    assert SENTINEL in redis.data
    assert PAUSED not in redis.data
    assert len(bus.errors("restore-guard")) == 1
    assert ex.orders == []

    # A second loss later is a new episode with its own alert.
    redis.data.clear()
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("restore-guard")) == 2


async def test_failed_write_keeps_the_sentinel_missing_and_retries(repo):
    """A write that did not land must not be forgotten: no sentinel, so
    the next pass writes again — without a second alert."""
    redis = FakeRedis()
    redis.fail_set.add(KILL)
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    await runner.startup()
    assert SENTINEL not in redis.data
    alerts = bus.errors("restore-guard")
    assert len(alerts) == 1 and "WRITE FAILED: kill switch" in alerts[0]

    await runner._run_reconcile("Periodic")
    assert len(bus.errors("restore-guard")) == 1
    assert SENTINEL not in redis.data

    redis.fail_set.clear()
    await runner._run_reconcile("Periodic")
    assert redis.data[KILL] == "1"
    assert SENTINEL in redis.data
    assert ex.orders == []


# --- guard 2, runner half ------------------------------------------------


async def test_reconcile_hold_blocks_pass_two_and_is_published(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)
    runner._orphan_first_seen[("ETH", "long")] = -1e9  # long confirmed

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == []
    assert result.held_orphans == ["ETH"]
    assert any(
        "HELD exchange-orphan long 2.0 ETH" in m and "reconcile hold" in m
        for m in bus.errors("reconcile")
    )


async def test_standing_hold_is_reannounced_at_most_every_30_minutes(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    runner, bus = _runner(repo, redis, FakeExchange())

    await runner._run_reconcile("Periodic")  # a restart announces it once
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 1
    assert "reconcile hold is still set" in bus.errors("reconcile")[0].lower()

    runner._hold_alert_at -= HOLD_REALERT_SECONDS + 1
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 2

    del redis.data[HOLD]  # a human cleared it
    await runner._run_reconcile("Periodic")
    assert runner._hold_alert_at is None
    assert len(bus.errors("reconcile")) == 2


async def test_orphans_held_by_the_guard_are_reannounced_too(repo):
    """Two orphans stay held with no reconcile hold set; the identical
    pass summary is deduplicated, so the reminder is what keeps them
    from going quiet after one alert."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    sol = Position(symbol="SOL", side="short", size=10.0, entry_price=150.0)
    runner, bus = _runner(repo, redis, FakeExchange([ETH, sol]), symbols=("ETH", "SOL"))

    await runner._run_reconcile("Periodic")
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 1  # the pass summary, once

    runner._hold_alert_at -= HOLD_REALERT_SECONDS + 1
    await runner._run_reconcile("Periodic")
    reminders = bus.errors("reconcile")[1:]
    assert len(reminders) == 1
    assert "ETH, SOL" in reminders[0]


async def test_orphan_closes_only_when_seen_again_four_minutes_later(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    first = await runner._run_reconcile("Periodic")
    assert ex.orders == []
    assert first.held_orphans == ["ETH"]

    await runner._run_reconcile("Periodic")  # seconds later: still held
    assert ex.orders == []

    runner._orphan_first_seen[("ETH", "long")] -= ORPHAN_CONFIRM_SECONDS
    await runner._run_reconcile("Periodic")
    assert ex.orders == [("ETH", "sell", 2.0)]


async def test_a_pass_that_could_not_look_restarts_the_count(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH])
    runner, _ = _runner(repo, redis, ex)

    await runner._run_reconcile("Periodic")
    runner._orphan_first_seen[("ETH", "long")] -= ORPHAN_CONFIRM_SECONDS
    ex.read_error = ExchangeReadError("502")
    await runner._run_reconcile("Periodic")  # skipped
    ex.read_error = None
    await runner._run_reconcile("Periodic")

    assert ex.orders == []


async def test_orphan_on_a_coin_no_strategy_here_trades_is_held(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH])
    runner, _ = _runner(repo, redis, ex, symbols=("BTC",))
    runner._orphan_first_seen[("ETH", "long")] = -1e9

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == []
    assert result.held_orphans == ["ETH"]


@pytest.mark.parametrize("key", [SENTINEL, HOLD])
async def test_redis_read_error_holds_and_the_tick_survives(repo, key):
    """Fail closed for pass 2; pass 1 and the tick still run."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.fail_get.add(key)
    await _row(repo)  # BTC row the exchange no longer has: pass 1 closes it
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)
    runner._orphan_first_seen[("ETH", "long")] = -1e9

    await runner.tick()

    assert ex.orders == []
    assert any("HELD exchange-orphan" in m for m in bus.errors("reconcile"))
    assert await repo.get_open_positions() == []  # the exit side still ran
    assert KILL not in redis.data  # a read error is not a lost state


# --- endpoint and keys ---------------------------------------------------


async def _call(app, method, path, **kw):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async with client.request(method, path, **kw) as resp:
            return resp.status, await resp.json()
    finally:
        await client.close()


async def test_endpoint_clears_and_reports_the_hold():
    redis = FakeRedis()
    redis.data[HOLD] = "1"
    app = web.Application()
    api_module._control_routes(
        app, control=_control(redis), exchange=None, strategies=[],
    )
    auth = {"X-Api-Key": "k"}
    with patch.object(api_module.settings, "api_key", "k"):
        path = "/api/control/reconcile-hold"
        assert (await _call(app, "GET", path))[0] == 401
        assert (await _call(app, "POST", path, json={"active": False}))[0] == 401
        assert await _call(app, "GET", path, headers=auth) == (
            200, {"reconcile_hold": True},
        )
        status, _ = await _call(app, "POST", path, headers=auth, json={"active": "false"})
        assert status == 400
        assert HOLD in redis.data
        assert await _call(app, "POST", path, headers=auth, json={"active": False}) == (
            200, {"reconcile_hold": False},
        )
        assert HOLD not in redis.data
        assert (await _call(app, "GET", path, headers=auth))[1] == {"reconcile_hold": False}


async def test_new_keys_carry_the_tenant_id():
    redis = FakeRedis()
    await _control(redis).write_sentinel()
    await _control(redis).set_reconcile_hold(True)
    assert set(redis.data) == {SENTINEL, HOLD}

    other = FakeRedis()
    await _control(other, tenant="").write_sentinel()
    await _control(other, tenant="").set_reconcile_hold(True)
    assert set(other.data) == {
        "hypertrade:testnet:control:sentinel",
        "hypertrade:testnet:control:reconcile_hold",
    }
