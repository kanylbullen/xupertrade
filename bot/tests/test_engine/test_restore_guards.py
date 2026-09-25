"""NU-2 restore guards, runner side (roadmap `docs/plans/next-level-roadmap.md`).

Guard 1: an empty Redis reads as "not paused, nothing disabled, no kill
switch". A missing sentinel means that state was lost, so the bot sets
its reconcile hold — it opens nothing and closes no exchange orphan,
exits keep running, never a pause (operator decision 5.4) — and alerts
once. The hold is per tenant, like the sentinel; the kill switch key is
shared by the whole mode and is left alone.

Guard 2's runner half: the hold, a failed Redis read and several orphans
at once decide what pass 2 may close. Several orphans also SET the hold,
so they stay held until a human clears it.

Real `Repository` on SQLite, real `BotControl` on a dict-backed Redis.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hypertrade import api as api_module
from hypertrade.config import settings
from hypertrade.db.repo import Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import HOLD_REALERT_SECONDS, EngineRunner
from hypertrade.events.bus import EventBus
from hypertrade.events.types import ErrorOccurred
from hypertrade.exchange.base import (
    Balance,
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)
from hypertrade.notify.telegram import TelegramNotifier

TENANT = "00000000-0000-4000-8000-000000000001"
OTHER_TENANT = "00000000-0000-4000-8000-000000000002"
PREFIX = f"hypertrade:testnet:t:{TENANT}:control:"
SENTINEL = PREFIX + "sentinel"
HOLD = PREFIX + "reconcile_hold"
KILL = "hypertrade:testnet:control:kill_switch"
PAUSED = "hypertrade:testnet:control:paused"
ETH = Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)
SOL = Position(symbol="SOL", side="short", size=10.0, entry_price=150.0)


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
    """`deliver=False` is the real bus answering that no subscriber got
    the event (Redis down, or the Telegram notifier re-subscribing)."""

    def __init__(self):
        self.events: list = []
        self.deliver = True

    async def publish(self, event):
        if not self.deliver:
            return False
        self.events.append(event)
        return True

    def errors(self, strategy: str) -> list[str]:
        return [
            e.message for e in self.events
            if isinstance(e, ErrorOccurred) and e.strategy == strategy
        ]


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", TENANT)
    monkeypatch.setattr(settings, "kill_switch", False)


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


def _control(redis: FakeRedis) -> BotControl:
    c = BotControl(redis_url="redis://unused/0", mode="testnet")
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


async def _row(repo, symbol="BTC", side="long"):
    await repo.open_position(
        strategy_name="kalman_breakout", symbol=symbol, side=side,
        size=0.5, entry_price=60000.0,
    )


async def _opens_allowed(runner) -> bool:
    return await runner.portfolio.check_risk_limits(is_open=True)


# --- guard 1 -----------------------------------------------------------


async def test_missing_sentinel_at_boot_sets_the_hold_and_alerts_once(repo):
    redis = FakeRedis()  # a flushed / freshly created Redis
    await _row(repo)
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    await runner.startup()

    assert HOLD in redis.data
    assert SENTINEL in redis.data
    assert KILL not in redis.data  # mode-wide: other tenants' bots read it
    assert PAUSED not in redis.data  # never pause: exits must keep running
    alerts = bus.errors("restore-guard")
    assert len(alerts) == 1
    assert "kalman_breakout long 0.5 BTC" in alerts[0]
    assert "NOT paused" in alerts[0]
    assert "/api/control/reconcile-hold" in alerts[0]
    assert f"redis-cli DEL {HOLD}" in alerts[0]  # shell path, exact key
    assert "market-closes a lone one" in alerts[0]  # what clearing does
    assert "kill-switch" not in alerts[0]
    # The startup pass held the untracked ETH position instead of closing it.
    assert ex.orders == []
    assert any("HELD exchange-orphan" in m for m in bus.errors("reconcile"))
    # Opens blocked, closes allowed.
    assert await _opens_allowed(runner) is False
    assert await runner.portfolio.check_risk_limits(is_open=False) is True

    # The next pass finds the sentinel: no second alert, still held.
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("restore-guard")) == 1
    assert ex.orders == []
    assert await _opens_allowed(runner) is False


async def test_present_sentinel_changes_nothing(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    runner, bus = _runner(repo, redis, FakeExchange())

    await runner.startup()

    assert HOLD not in redis.data
    assert bus.errors("restore-guard") == []
    assert await _opens_allowed(runner) is True


async def test_redis_lost_mid_run_is_caught_on_the_next_tick(repo):
    """Not only on the 5-minute reconcile: the tick right after the loss
    holds opens before any strategy runs."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    runner, bus = _runner(repo, redis, FakeExchange())
    await runner.startup()
    runner._last_reconcile = time.time()  # no periodic pass this tick
    runner._last_funding_poll = time.time()
    seen = []

    async def run_strategy(strategy):
        seen.append(await _opens_allowed(runner))

    runner._run_strategy = run_strategy

    redis.data.clear()  # FLUSHALL, or redis recreated on an empty volume
    await runner.tick()

    assert seen == [False]  # the strategy ran, but could not open
    assert HOLD in redis.data
    assert SENTINEL in redis.data
    assert len(bus.errors("restore-guard")) == 1

    # A second loss later is a new episode with its own alert.
    redis.data.clear()
    await runner.tick()
    assert len(bus.errors("restore-guard")) == 2


async def test_one_tenants_lost_state_does_not_stop_another_tenants_bot(
    repo, monkeypatch,
):
    """The bots of every tenant share one Redis. A new tenant's bot (no
    sentinel yet) must not switch off the operator's opens."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    runner_a, bus_a = _runner(repo, redis, FakeExchange())
    monkeypatch.setattr(settings, "tenant_id", OTHER_TENANT)
    runner_b, bus_b = _runner(repo, redis, FakeExchange())

    await runner_b.startup()
    await runner_a._check_control_state()

    assert KILL not in redis.data
    assert await _opens_allowed(runner_a) is True
    assert bus_a.errors("restore-guard") == []
    assert await _opens_allowed(runner_b) is False
    assert len(bus_b.errors("restore-guard")) == 1
    assert HOLD not in redis.data  # B's hold carries B's tenant id


async def test_failed_hold_write_holds_in_memory_and_retries(repo):
    """A hold that did not land still blocks opens, is retried, and the
    sentinel waits for it — without a second alert."""
    redis = FakeRedis()
    redis.fail_set.add(HOLD)
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    await runner.startup()
    assert SENTINEL not in redis.data
    alerts = bus.errors("restore-guard")
    assert len(alerts) == 1 and "WRITE FAILED" in alerts[0]
    assert await _opens_allowed(runner) is False
    assert ex.orders == []

    await runner._check_control_state()
    assert len(bus.errors("restore-guard")) == 1
    assert await _opens_allowed(runner) is False

    redis.fail_set.clear()
    await runner._check_control_state()
    assert HOLD in redis.data
    assert SENTINEL in redis.data
    assert await _opens_allowed(runner) is False  # now held by Redis
    assert ex.orders == []


async def test_undelivered_alert_is_sent_again_until_someone_gets_it(repo):
    """The writes landed and the sentinel is back, but nobody received
    the alert: it goes out on the next check, and only once."""
    redis = FakeRedis()
    runner, bus = _runner(repo, redis, FakeExchange())
    bus.deliver = False

    await runner.startup()
    assert SENTINEL in redis.data
    assert bus.errors("restore-guard") == []

    bus.deliver = True
    await runner._check_control_state()
    await runner._check_control_state()
    assert len(bus.errors("restore-guard")) == 1


# --- guard 2, runner half ------------------------------------------------


async def test_reconcile_hold_blocks_pass_two_and_opens(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == []
    assert result.held_orphans == ["ETH"]
    assert any(
        "HELD exchange-orphan long 2.0 ETH" in m and "reconcile hold" in m
        for m in bus.errors("reconcile")
    )
    assert await _opens_allowed(runner) is False


async def test_several_orphans_set_the_hold_and_the_survivor_stays_held(repo):
    """A count that drops to one — a human closed one of them, or a
    liquidation did — must not release the other to a market close.
    Only a human clearing the hold does."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH, SOL])
    runner, bus = _runner(repo, redis, ex, symbols=("ETH", "SOL"))

    await runner._run_reconcile("Periodic")
    assert ex.orders == []
    assert HOLD in redis.data
    assert await _opens_allowed(runner) is False  # nothing nets into them
    notice = bus.errors("reconcile")[-1]
    assert "Reconcile hold SET" in notice and f"redis-cli DEL {HOLD}" in notice

    ex.positions = [ETH]  # a human closed SOL on the exchange
    result = await runner._run_reconcile("Periodic")
    assert ex.orders == []
    assert result.held_orphans == ["ETH"]

    del redis.data[HOLD]  # the human acknowledges
    await runner._run_reconcile("Periodic")
    assert ex.orders == [("ETH", "sell", 2.0)]
    assert await _opens_allowed(runner) is True


async def test_orphans_on_coins_nobody_here_trades_are_held_not_escalated(repo):
    """No strategy here can net into them, so no hold and no open block;
    the reminder keeps them from going quiet after one alert."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH, SOL])
    runner, bus = _runner(repo, redis, ex, symbols=("BTC",))

    await runner._run_reconcile("Periodic")
    await runner._run_reconcile("Periodic")
    assert ex.orders == []
    assert HOLD not in redis.data
    assert await _opens_allowed(runner) is True
    assert len(bus.errors("reconcile")) == 1  # the pass summary, once

    runner._hold_alert_at -= HOLD_REALERT_SECONDS + 1
    await runner._run_reconcile("Periodic")
    reminders = bus.errors("reconcile")[1:]
    assert len(reminders) == 1
    assert "ETH, SOL" in reminders[0]


async def test_symbol_freed_by_a_wrong_side_close_is_closed_in_the_same_pass(repo):
    """Pass 1 closes the long row the exchange shows as short; pass 2
    closes the now-untracked short in the SAME pass, before a strategy
    OPEN in this tick can net against it."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    await _row(repo, "BTC", "long")
    short = Position(symbol="BTC", side="short", size=0.5, entry_price=61000.0)
    ex = FakeExchange([short])
    runner, bus = _runner(repo, redis, ex, symbols=("BTC",))

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == [("BTC", "buy", 0.5)]
    assert result.held_orphans == []
    assert HOLD not in redis.data


async def test_standing_hold_is_reannounced_at_most_every_30_minutes(repo):
    """A hold set by hand after a restore (the sentinel came back with
    Redis, so guard 1 never fires) is announced by this reminder only."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    runner, bus = _runner(repo, redis, FakeExchange())

    await runner._run_reconcile("Periodic")  # a restart announces it once
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 1
    assert "reconcile hold is still set" in bus.errors("reconcile")[0].lower()
    assert "opens nothing" in bus.errors("reconcile")[0]

    runner._hold_alert_at -= HOLD_REALERT_SECONDS + 1
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 2

    del redis.data[HOLD]  # a human cleared it
    await runner._run_reconcile("Periodic")
    assert runner._hold_alert_at is None
    assert len(bus.errors("reconcile")) == 2


async def test_notices_that_do_not_name_the_hold_do_not_silence_it(repo):
    """HL reads flapping: every skip notice is new and gets published,
    but none of them says the hold is set — the reminder still comes."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    ex = FakeExchange()
    runner, bus = _runner(repo, redis, ex)
    await runner._run_reconcile("Periodic")  # announces the hold
    runner._hold_alert_at -= HOLD_REALERT_SECONDS + 1

    ex.read_error = ExchangeReadError("502 Bad Gateway")
    await runner._run_reconcile("Periodic")

    new = bus.errors("reconcile")[1:]
    assert any("skipped" in m for m in new)
    assert any("still set" in m for m in new)


async def test_undelivered_reconcile_notice_is_sent_again(repo):
    """An undelivered HELD notice is neither deduplicated away nor
    counted as the hold's announcement."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    runner, bus = _runner(repo, redis, FakeExchange([ETH]))
    bus.deliver = False
    await runner._run_reconcile("Periodic")
    assert runner._hold_alert_at is None

    bus.deliver = True
    await runner._run_reconcile("Periodic")
    assert any("HELD exchange-orphan" in m for m in bus.errors("reconcile"))


async def test_orphan_on_a_coin_no_strategy_here_trades_is_held(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH])
    runner, _ = _runner(repo, redis, ex, symbols=("BTC",))

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == []
    assert result.held_orphans == ["ETH"]
    assert HOLD not in redis.data


@pytest.mark.parametrize("key", [SENTINEL, HOLD])
async def test_redis_read_error_holds_and_the_tick_survives(repo, key):
    """Fail closed for pass 2 and for opens; pass 1 and the tick still run."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.fail_get.add(key)
    await _row(repo)  # BTC row the exchange no longer has: pass 1 closes it
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex)

    await runner.tick()

    assert ex.orders == []
    assert any("HELD exchange-orphan" in m for m in bus.errors("reconcile"))
    assert await repo.get_open_positions() == []  # the exit side still ran
    assert HOLD not in redis.data  # a read error is not a lost state
    assert await _opens_allowed(runner) is False


# --- delivery ------------------------------------------------------------


async def test_event_bus_counts_only_a_publish_someone_received():
    bus = EventBus(redis_url="redis://unused:6379/0")
    bus._redis = MagicMock()
    bus._redis.publish = AsyncMock(return_value=0)
    assert await bus.publish(ErrorOccurred(strategy="x", message="y")) is False
    bus._redis.publish = AsyncMock(return_value=1)
    assert await bus.publish(ErrorOccurred(strategy="x", message="y")) is True


class _PubSub:
    def __init__(self, fail: bool):
        self.fail = fail

    async def subscribe(self, *channels):
        pass

    async def listen(self):
        if self.fail:
            raise ConnectionError("Connection closed by server.")
        yield {
            "type": "message",
            "data": json.dumps({"type": "error", "strategy": "restore-guard",
                                "message": "lost", "mode": "testnet"}),
        }
        await asyncio.Event().wait()

    async def unsubscribe(self):
        if self.fail:
            raise ConnectionError("Connection closed by server.")

    async def close(self):
        pass


async def test_telegram_forwarding_survives_a_dropped_redis_connection():
    """The listener re-subscribes instead of dying, so the restore-guard
    alert sent after Redis came back still reaches Telegram."""
    notifier = TelegramNotifier(token="t", chat_id="1")
    notifier._resubscribe_delay = 0
    pubsubs = iter([_PubSub(fail=True), _PubSub(fail=False)])
    notifier._redis = SimpleNamespace(pubsub=lambda: next(pubsubs))
    sent = asyncio.Event()
    notifier.send = AsyncMock(side_effect=lambda text: sent.set())

    task = asyncio.create_task(notifier._event_loop())
    try:
        await asyncio.wait_for(sent.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "lost" in notifier.send.await_args.args[0]


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


async def test_new_keys_carry_the_tenant_id(monkeypatch):
    redis = FakeRedis()
    await _control(redis).write_sentinel()
    await _control(redis).set_reconcile_hold(True)
    assert set(redis.data) == {SENTINEL, HOLD}

    monkeypatch.setattr(settings, "tenant_id", None)
    other = FakeRedis()
    await _control(other).write_sentinel()
    await _control(other).set_reconcile_hold(True)
    assert set(other.data) == {
        "hypertrade:testnet:control:sentinel",
        "hypertrade:testnet:control:reconcile_hold",
    }
