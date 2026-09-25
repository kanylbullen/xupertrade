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
from hypertrade.db import repo as repo_module
from hypertrade.db.repo import Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import HOLD_REALERT_SECONDS, EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
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
        self.fail_delete: set[str] = set()

    async def get(self, key):
        if key in self.fail_get:
            raise ConnectionError(f"GET {key} failed")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        if key in self.fail_set:
            raise ConnectionError(f"SET {key} failed")
        self.data[key] = value

    async def delete(self, key):
        if key in self.fail_delete:
            raise ConnectionError(f"DEL {key} failed")
        self.data.pop(key, None)

    async def smembers(self, key):
        return set()

    async def hgetall(self, key):
        return {}


class FakeExchange:
    """Nets every fill into `positions`, like HL's one position per coin.
    `fill_cap` makes an order fill at most that much (an IOC that fills
    short)."""

    def __init__(self, positions=()):
        self.positions = list(positions)
        self.read_error: Exception | None = None
        self.orders: list[tuple] = []
        self.fill_cap: float | None = None

    async def get_positions(self):
        if self.read_error is not None:
            raise self.read_error
        return list(self.positions)

    async def get_position(self, symbol):
        return next((p for p in self.positions if p.symbol == symbol), None)

    def get_size_precision(self, symbol):
        return 4

    def _net(self, symbol, delta):
        cur = next((p for p in self.positions if p.symbol == symbol), None)
        signed = (cur.size if cur.side == "long" else -cur.size) if cur else 0.0
        new = signed + delta
        self.positions = [p for p in self.positions if p.symbol != symbol]
        if abs(new) > 1e-9:
            self.positions.append(Position(
                symbol=symbol, side="long" if new > 0 else "short",
                size=abs(new), entry_price=2000.0,
            ))

    async def get_balance(self):
        return Balance(total=1000.0, available=1000.0)

    async def fetch_user_fills(self, address=None, since_ms=None):
        return []

    async def get_current_price(self, symbol):
        return 2100.0

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET):
        self.orders.append((symbol, side, size))
        filled = min(size, self.fill_cap) if self.fill_cap else size
        self._net(symbol, filled if side == "buy" else -filled)
        return Order(
            id=f"order-{len(self.orders)}", symbol=symbol, side=side,
            size=filled, order_type=order_type, filled_price=2100.0,
            status=OrderStatus.FILLED,
        )


class RoundingExchange(FakeExchange):
    """Rounds every order to 4 dp, to nearest, as the HL wrapper rounds
    to szDecimals — so a close can fill more than its row asked."""

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET):
        return await super().place_order(symbol, side, round(size, 4), order_type)


class Strat:
    """What `_execute_signal` needs of a strategy; `side` is the position
    it believes in."""

    def __init__(self, symbol):
        self.name, self.symbol, self.leverage = f"s_{symbol}", symbol, 1
        self.side: str | None = None

    def export_state(self):
        return {"side": self.side} if self.side else None

    def restore_state(self, side, entry):
        self.side = side

    def restore_from_json(self, side, entry, state):
        self.side = side

    def reset_position(self):
        self.side = None

    def on_filled(self, side, price):
        pass


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
    # Reconcile's confirming re-reads wait 2 s each; the reads are faked.
    monkeypatch.setattr(repo_module, "asyncio", SimpleNamespace(sleep=AsyncMock()))


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


def _runner(repo, redis, exchange, *, symbols=("ETH",), first_pass=False):
    """`first_pass=True` is a bot that just booted: its first reconcile
    pass holds every orphan. Otherwise it has been running a while."""
    bus = Bus()
    runner = EngineRunner(
        exchange=exchange, strategies=[Strat(s) for s in symbols],
        repo=repo, event_bus=bus, control=_control(redis),
    )
    runner._restore_read_backoff = ()
    runner._orphans_checked = not first_pass
    return runner, bus


async def _row(repo, symbol="BTC", side="long", *, strategy="kalman_breakout", size=0.5):
    await repo.open_position(
        strategy_name=strategy, symbol=symbol, side=side,
        size=size, entry_price=60000.0,
    )


def _signal(action, symbol="ETH"):
    return Signal(action=action, symbol=symbol, strategy_name=f"s_{symbol}")


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
    assert "market-closes a lone exchange position" in alerts[0]  # what clearing does
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
    """No strategy here can net into them, so no hold and no open block.
    A human's standing position there is announced once, not every 30
    minutes forever; a change in its size or side is news again."""
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
    assert runner._hold_alert_at is None

    await runner._run_reconcile("Periodic")  # 30 min later, nothing new
    assert len(bus.errors("reconcile")) == 1

    ex.positions = [ETH, Position(symbol="SOL", side="short", size=12.0, entry_price=150.0)]
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 2
    assert "short 12.0 SOL" in bus.errors("reconcile")[1]


async def test_a_humans_untraded_position_does_not_escalate_our_leftover(repo):
    """Only orphans on traded coins count as several: the lone ETH
    leftover is closed, the DOGE someone holds by hand is left alone,
    and opens stay on."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    doge = Position(symbol="DOGE", side="long", size=100.0, entry_price=0.1)
    ex = FakeExchange([doge, ETH])
    runner, _ = _runner(repo, redis, ex)

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == [("ETH", "sell", 2.0)]
    assert result.held_orphans == ["DOGE"]
    assert HOLD not in redis.data
    assert await _opens_allowed(runner) is True


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


async def test_first_pass_after_boot_holds_a_lone_orphan_and_sets_the_hold(repo):
    """A restore with Redis intact (the sentinel came back with it) or a
    new key: the first pass holds even one orphan and sets the hold, so a
    human decides. A paused pass does not count as that first pass."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[PAUSED] = "1"  # the runbook: boot paused
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex, first_pass=True)

    await runner.startup()
    assert HOLD not in redis.data and ex.orders == []

    del redis.data[PAUSED]
    result = await runner._run_reconcile("Periodic")
    assert ex.orders == []
    assert result.held_orphans == ["ETH"]
    assert "first reconcile pass" in bus.errors("reconcile")[-1]
    assert "Reconcile hold SET: ETH" in bus.errors("reconcile")[-1]
    assert HOLD in redis.data
    assert await _opens_allowed(runner) is False

    del redis.data[HOLD]  # a human checked the book
    await runner._run_reconcile("Periodic")
    assert ex.orders == [("ETH", "sell", 2.0)]


async def test_clearing_the_hold_reconciles_before_any_strategy_runs(repo):
    """The tick that sees the hold cleared runs reconcile first, so the
    survivor is closed before a strategy can open into it — even though
    the periodic pass is not due."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH, SOL])
    runner, _ = _runner(repo, redis, ex, symbols=("ETH", "SOL"))
    await runner._run_reconcile("Periodic")
    assert HOLD in redis.data

    ex.positions = [ETH]  # a human closed SOL, leaves ETH to the bot
    del redis.data[HOLD]
    runner._last_reconcile = time.time()  # periodic pass not due
    runner._last_funding_poll = time.time()
    seen = []

    async def run_strategy(strategy):
        seen.append((await _opens_allowed(runner), list(ex.orders)))

    runner._run_strategy = run_strategy
    await runner.tick()

    assert seen == [(True, [("ETH", "sell", 2.0)])] * 2


async def test_opens_wait_for_the_first_unpaused_pass_and_the_tick_runs_it(repo):
    """The runbook after a restore: boot paused, Redis intact. The
    startup pass and the paused ticks are dry runs, and each moves the
    5-minute clock. The first unpaused tick still runs pass 2 before any
    strategy, and it holds the orphan — no strategy could open into it
    at any point."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[PAUSED] = "1"
    ex = FakeExchange([ETH])
    runner, bus = _runner(repo, redis, ex, first_pass=True)
    await runner.startup()
    runner._last_funding_poll = time.time()
    seen = []

    async def run_strategy(strategy):
        seen.append((await _opens_allowed(runner), HOLD in redis.data))

    runner._run_strategy = run_strategy
    await runner.tick()  # paused: a dry run
    assert seen == [] and HOLD not in redis.data
    assert await _opens_allowed(runner) is False

    del redis.data[PAUSED]
    await runner.tick()  # the periodic pass is not due

    assert seen == [(False, True)]
    assert ex.orders == []
    assert "Reconcile hold SET: ETH" in bus.errors("reconcile")[-1]


async def test_the_first_unpaused_tick_opens_once_its_pass_found_nothing(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[PAUSED] = "1"
    runner, _ = _runner(repo, redis, FakeExchange(), first_pass=True)
    await runner.startup()
    await runner.tick()
    runner._last_funding_poll = time.time()
    seen = []

    async def run_strategy(strategy):
        seen.append(await _opens_allowed(runner))

    runner._run_strategy = run_strategy
    del redis.data[PAUSED]
    await runner.tick()

    assert seen == [True]


async def test_a_skipped_pass_after_a_clear_keeps_opens_waiting(repo):
    """The pass a clear forces reads the exchange; when that read fails
    the pass is skipped, opens keep waiting, and the next tick tries
    again instead of waiting 5 minutes."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    ex = FakeExchange([ETH])
    runner, _ = _runner(repo, redis, ex)
    await runner._check_control_state()
    runner._last_reconcile = time.time()
    runner._last_funding_poll = time.time()
    seen = []

    async def run_strategy(strategy):
        seen.append((await _opens_allowed(runner), list(ex.orders)))

    runner._run_strategy = run_strategy
    del redis.data[HOLD]
    ex.read_error = ExchangeReadError("502 Bad Gateway")
    await runner.tick()
    ex.read_error = None
    await runner.tick()

    assert seen == [(False, []), (True, [("ETH", "sell", 2.0)])]


async def test_a_hold_set_again_after_a_clear_is_announced(repo):
    """Cleared while both orphans remain: the next pass sets the hold
    again with the same summary, which must not be deduplicated away."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH, SOL])
    runner, bus = _runner(repo, redis, ex, symbols=("ETH", "SOL"))
    await runner._run_reconcile("Periodic")
    assert len(bus.errors("reconcile")) == 1

    await runner.control.set_reconcile_hold(False)
    await runner._run_reconcile("Periodic")

    assert HOLD in redis.data
    assert ex.orders == []
    assert len(bus.errors("reconcile")) == 2
    assert "Reconcile hold SET" in bus.errors("reconcile")[1]


async def test_escalation_whose_hold_write_failed_still_holds(repo):
    """The survivor stays held in memory while the write fails, and the
    write lands once Redis takes it."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.fail_set.add(HOLD)
    ex = FakeExchange([ETH, SOL])
    runner, _ = _runner(repo, redis, ex, symbols=("ETH", "SOL"))

    await runner._run_reconcile("Periodic")
    assert runner.control.hold_unwritten
    ex.positions = [ETH]
    result = await runner._run_reconcile("Periodic")
    assert ex.orders == [] and result.held_orphans == ["ETH"]
    assert await _opens_allowed(runner) is False

    redis.fail_set.clear()
    await runner._check_control_state()
    assert HOLD in redis.data and not runner.control.hold_unwritten


async def test_without_bot_control_orphans_are_held_and_opens_allowed(repo):
    ex = FakeExchange([ETH])
    runner = EngineRunner(exchange=ex, strategies=[Strat("ETH")], repo=repo)
    runner._orphans_checked = True

    result = await runner._run_reconcile("Periodic")

    assert ex.orders == [] and result.held_orphans == ["ETH"]
    assert await _opens_allowed(runner) is True


async def test_bot_control_without_redis_reads_as_held():
    control = BotControl(redis_url="redis://unused/0", mode="testnet")
    assert await control.is_reconcile_hold_active() is True
    assert await control.sentinel_present() is True


# --- the order path under the hold -------------------------------------


async def _held_runner(repo, positions=(ETH,)):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    ex = FakeExchange(positions)
    runner, bus = _runner(repo, redis, ex)
    await runner._check_control_state()
    return runner, bus, ex


async def test_hold_refuses_an_open_in_the_order_path_and_says_why(repo):
    runner, bus, ex = await _held_runner(repo, positions=())

    ok = await runner._execute_signal(_signal(SignalAction.OPEN_LONG), 2100.0)

    assert ok is False
    assert ex.orders == []
    assert await repo.get_open_positions() == []
    assert runner._refusal_notes[("s_ETH", "ETH")][0] == "reconcile hold"
    assert bus.errors("s_ETH") == []  # the hold is announced on its own


async def test_hold_lets_a_close_through_the_order_path(repo):
    runner, _, ex = await _held_runner(repo)
    await _row(repo, "ETH", strategy="s_ETH", size=2.0)

    ok = await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0)

    assert ok is True
    assert ex.orders == [("ETH", "sell", 2.0)]
    assert await repo.get_open_positions() == []


async def test_hold_lets_a_flip_close_but_refuses_its_open(repo):
    runner, bus, ex = await _held_runner(repo)
    await _row(repo, "ETH", strategy="s_ETH", size=2.0)

    ok = await runner._execute_signal(_signal(SignalAction.OPEN_SHORT), 2100.0)

    assert ok is False
    assert ex.orders == [("ETH", "sell", 2.0)]  # the close half only
    assert ex.positions == []
    assert await repo.get_open_positions() == []
    [note] = bus.errors("s_ETH")
    assert note.startswith("Reconcile hold (reconcile hold is set)")
    assert f"redis-cli DEL {HOLD}" in note and "Risk limit" not in note


async def test_short_filled_close_under_the_hold_keeps_the_rest_on_its_row(repo):
    """The unfilled rest must not become an orphan the hold strands: it
    stays on the row, the strategy still owns it, and its next exit
    closes it."""
    runner, _, ex = await _held_runner(repo)
    await _row(repo, "ETH", strategy="s_ETH", size=2.0)
    strat = runner.strategies[0]
    strat.side = "long"
    ex.fill_cap = 1.5

    ok = await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0)

    assert ok is False  # not finished
    [row] = await repo.get_open_positions()
    assert row.size == pytest.approx(0.5)
    assert strat.side == "long"  # re-synced to the rest
    result = await runner._run_reconcile("Periodic")
    assert result.held_orphans == [] and len(ex.orders) == 1

    ex.fill_cap = None
    ok = await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0)
    assert ok is True
    assert ex.orders[-1] == ("ETH", "sell", pytest.approx(0.5))
    assert ex.positions == [] and await repo.get_open_positions() == []


async def test_short_filled_flip_close_opens_nothing(repo):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH])
    runner, _ = _runner(repo, redis, ex)
    runner._ensure_leverage_pushed = AsyncMock(return_value=True)
    await _row(repo, "ETH", strategy="s_ETH", size=2.0)
    ex.fill_cap = 1.0

    ok = await runner._execute_signal(_signal(SignalAction.OPEN_SHORT), 2100.0)

    assert ok is False
    assert ex.orders == [("ETH", "sell", 2.0)]  # no open after the close
    [row] = await repo.get_open_positions()
    assert (row.side, row.size) == ("long", pytest.approx(1.0))
    assert runner.strategies[0].side == "long"


async def _rounding_runner(repo, size_on_exchange):
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = RoundingExchange([Position(
        symbol="ETH", side="long", size=size_on_exchange, entry_price=2000.0,
    )])
    runner, bus = _runner(repo, redis, ex)
    runner._ensure_leverage_pushed = AsyncMock(return_value=True)
    return runner, bus, ex


async def test_a_short_fill_rest_is_rounded_and_its_close_finishes(repo):
    """The rest goes on the row rounded to szDecimals — 0.2, not the
    0.19999999999999998 of 0.3 - 0.1 — and the exit that closes it is a
    finished close: row closed, strategy left flat, True."""
    runner, _, ex = await _rounding_runner(repo, 0.3)
    await _row(repo, "ETH", strategy="s_ETH", size=0.3)
    strat = runner.strategies[0]
    ex.fill_cap = 0.1

    assert await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0) is False
    [row] = await repo.get_open_positions()
    assert row.size == 0.2

    ex.fill_cap = None
    strat.side = None  # a strategy that exits goes flat itself
    ok = await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0)

    assert ok is True
    assert await repo.get_open_positions() == [] and ex.positions == []
    assert strat.side is None


@pytest.mark.parametrize("row_size", [0.49996, 0.19999999999999998])
async def test_a_close_that_rounds_up_past_a_legacy_row_finishes(repo, row_size):
    """A row from before the fill-size fix holds the unrounded request;
    the exchange rounds its close up and fills more than the row. That
    is not a remainder: the row closes and nothing re-syncs to it."""
    runner, _, ex = await _rounding_runner(repo, round(row_size, 4))
    await _row(repo, "ETH", strategy="s_ETH", size=row_size)

    ok = await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0)

    assert ok is True
    assert ex.orders == [("ETH", "sell", round(row_size, 4))]
    assert await repo.get_open_positions() == [] and ex.positions == []
    assert runner.strategies[0].side is None


async def test_a_flip_whose_close_rounds_up_opens_its_new_side(repo):
    runner, bus, ex = await _rounding_runner(repo, 0.5)
    await _row(repo, "ETH", strategy="s_ETH", size=0.49996)

    ok = await runner._execute_signal(_signal(SignalAction.OPEN_SHORT), 2100.0)

    assert ok is True
    assert ex.orders[0] == ("ETH", "sell", 0.5)
    assert len(ex.orders) == 2 and ex.orders[1][1] == "sell"
    [row] = await repo.get_open_positions()
    assert row.side == "short"
    assert not any("Flip-close failed" in m for m in bus.errors("s_ETH"))


async def test_a_one_step_short_fill_is_a_remainder_not_rounding(repo):
    """Rounding moves a size by half a step at most; an order that
    filled a whole step short left that step on the exchange, and it
    stays on the row."""
    runner, _, ex = await _rounding_runner(repo, 2.0)
    await _row(repo, "ETH", strategy="s_ETH", size=2.0)
    ex.fill_cap = 1.9999

    ok = await runner._execute_signal(_signal(SignalAction.CLOSE_LONG), 2100.0)

    assert ok is False
    [row] = await repo.get_open_positions()
    assert row.size == 0.0001
    assert runner.strategies[0].side == "long"


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


def _app(control):
    app = web.Application()
    api_module._control_routes(app, control=control, exchange=None, strategies=[])
    return app


AUTH = {"X-Api-Key": "k"}
HOLD_PATH = "/api/control/reconcile-hold"


async def test_endpoint_answers_a_redis_error_with_a_structured_503():
    redis = FakeRedis()
    redis.data[HOLD] = "1"
    redis.fail_delete.add(HOLD)
    redis.fail_get.add(HOLD)
    app = _app(_control(redis))
    with patch.object(api_module.settings, "api_key", "k"):
        assert await _call(
            app, "POST", HOLD_PATH, headers=AUTH, json={"active": False},
        ) == (503, {"error": "Redis error: ConnectionError"})
        assert await _call(app, "GET", HOLD_PATH, headers=AUTH) == (
            503, {"error": "Redis error: ConnectionError"},
        )
    assert HOLD in redis.data


async def test_an_api_clear_releases_a_hold_the_runner_set(repo):
    """The runner and the endpoint share one BotControl: a POST clear,
    not only a `redis-cli DEL`, lets the lone survivor be closed. Opens
    wait for that pass."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    ex = FakeExchange([ETH, SOL])
    runner, _ = _runner(repo, redis, ex, symbols=("ETH", "SOL"))
    await runner._run_reconcile("Periodic")
    assert await _opens_allowed(runner) is False

    ex.positions = [ETH]
    with patch.object(api_module.settings, "api_key", "k"):
        assert (await _call(
            _app(runner.control), "POST", HOLD_PATH, headers=AUTH,
            json={"active": False},
        ))[0] == 200
    await runner._check_control_state()
    assert await _opens_allowed(runner) is False
    await runner._run_reconcile("Periodic")
    assert ex.orders == [("ETH", "sell", 2.0)]
    assert await _opens_allowed(runner) is True


async def test_an_api_clear_ends_a_hold_whose_write_failed(repo):
    """A hold held only in memory is reported by GET and ended by a POST
    clear; the runner does not write it back once Redis recovers."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.fail_set.add(HOLD)
    ex = FakeExchange([ETH, SOL])
    runner, _ = _runner(repo, redis, ex, symbols=("ETH", "SOL"))
    await runner._run_reconcile("Periodic")
    assert HOLD not in redis.data and runner.control.hold_unwritten

    app = _app(runner.control)
    ex.positions = []  # the human closed both
    redis.fail_set.clear()  # and Redis takes writes again
    with patch.object(api_module.settings, "api_key", "k"):
        assert await _call(app, "GET", HOLD_PATH, headers=AUTH) == (
            200, {"reconcile_hold": True},
        )
        assert (await _call(
            app, "POST", HOLD_PATH, headers=AUTH, json={"active": False},
        ))[0] == 200
        assert await _call(app, "GET", HOLD_PATH, headers=AUTH) == (
            200, {"reconcile_hold": False},
        )
    await runner._check_control_state()
    assert HOLD not in redis.data
    await runner._run_reconcile("Periodic")
    assert await _opens_allowed(runner) is True


async def test_a_queued_alert_says_so_once_the_hold_was_cleared(repo):
    """Undelivered until after a human cleared the hold: what arrives
    says the state was lost and the hold is gone, not that it holds —
    and still what the loss reset, which the clear did not undo."""
    redis = FakeRedis()
    runner, bus = _runner(repo, redis, FakeExchange())
    bus.deliver = False
    await runner.startup()
    await runner.control.set_reconcile_hold(False)

    bus.deliver = True
    await runner._check_control_state()

    [alert] = bus.errors("restore-guard")
    assert "has since been cleared" in alert and "UTC" in alert
    assert "daily-loss counter were reset" in alert
    assert "Reconcile hold ON" not in alert
    await runner._run_reconcile("Periodic")
    assert await _opens_allowed(runner) is True


async def test_a_clear_after_a_flush_leaves_guard_one_to_fire(repo):
    """Redis emptied, and a clear lands before the runner's next check:
    the clear must not write the sentinel, or the lost state — disabled
    strategies back on — is never noticed."""
    redis = FakeRedis()
    redis.data[SENTINEL] = "1"
    redis.data[HOLD] = "1"
    runner, bus = _runner(repo, redis, FakeExchange())
    await runner._check_control_state()

    redis.data.clear()
    await runner.control.set_reconcile_hold(False)
    assert SENTINEL not in redis.data

    await runner._check_control_state()
    assert HOLD in redis.data and SENTINEL in redis.data
    assert len(bus.errors("restore-guard")) == 1
    assert await _opens_allowed(runner) is False


async def test_a_clear_acknowledges_a_loss_whose_sentinel_write_failed(repo):
    """The hold landed, the sentinel did not: a human clear acknowledges
    the loss the runner already announced, so the hold stays cleared."""
    redis = FakeRedis()
    redis.fail_set.add(SENTINEL)
    runner, bus = _runner(repo, redis, FakeExchange())
    await runner._check_control_state()
    assert HOLD in redis.data and SENTINEL not in redis.data

    redis.fail_set.clear()
    await runner.control.set_reconcile_hold(False)
    await runner._check_control_state()

    assert SENTINEL in redis.data and HOLD not in redis.data
    assert len(bus.errors("restore-guard")) == 1


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
