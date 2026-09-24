"""NU-2 guards in the runner: a lost Redis or a restored DB must not
re-arm or liquidate.

Every BotControl key reads as a harmless default when it is missing:
not paused, nothing disabled, no kill switch. A flushed Redis, or a
redis container recreated on an empty volume, therefore used to bring
every disabled strategy back with nothing said. The runner checks a
sentinel key at boot and on every tick, and the DB against the
exchange's fills at every boot:

- sentinel missing → kill switch ON (opens blocked, exits keep
  running), one alert listing the open positions, then the sentinel is
  written;
- a stale DB (exchange fills no trades row records), sentinel or not:
  a Redis restored from a snapshot still has the sentinel → kill switch
  AND PAUSED, with the alert repeated every DR_FREEZE_REALERT_MINUTES
  while frozen. The guard never pauses on a missing sentinel alone
  (decision 5.4: pause freezes exits too);
- an exchange orphan reconcile holds stays held until a human resolves
  it, and opens on its coin are refused meanwhile.

Real BotControl over an in-memory Redis stand-in with fault injection,
real PortfolioManager, real Repository on SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.db import models
from hypertrade.db.repo import ReconcileResult, Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import EngineRunner
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import (
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)

MODE = "testnet"
SENTINEL = f"hypertrade:{MODE}:control:sentinel"
KILL = f"hypertrade:{MODE}:control:kill_switch"
PAUSED = f"hypertrade:{MODE}:control:paused"
FREEZE = f"hypertrade:{MODE}:control:dr_freeze"
NOW = datetime.now(timezone.utc)


class FlakyRedis:
    """Just enough of redis.asyncio for BotControl, plus faults.

    `fail_reads` / `fail_writes` make every read / write raise, the way a
    down Redis (or one at maxmemory refusing writes) does. `fail_keys`
    fails the writes of those keys only (a connection reset mid-sequence).
    """

    def __init__(self, data=None):
        self.data: dict = dict(data or {})
        self.fail_reads = False
        self.fail_writes = False
        self.fail_keys: set[str] = set()

    def _read(self):
        if self.fail_reads:
            raise ConnectionError("redis down")

    def _write(self, key):
        if self.fail_writes or key in self.fail_keys:
            raise ConnectionError("OOM command not allowed")

    async def get(self, key):
        self._read()
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self._write(key)
        self.data[key] = value

    async def delete(self, key):
        self._write(key)
        self.data.pop(key, None)

    async def smembers(self, key):
        self._read()
        return set(self.data.get(key, set()))

    async def sismember(self, key, member):
        self._read()
        return member in self.data.get(key, set())

    async def hgetall(self, key):
        self._read()
        return dict(self.data.get(key, {}))

    async def hget(self, key, field):
        self._read()
        return self.data.get(key, {}).get(field)


class FakeExchange:
    """`fills_fail` makes the fills read fail the way the HL wrapper
    does: [] by default, ExchangeReadError with `raise_on_error`."""

    def __init__(self, positions=(), fills=()):
        self.positions = list(positions)
        self.fills = list(fills)
        self.fills_fail = False
        self.orders: list[tuple] = []

    async def get_positions(self):
        return list(self.positions)

    async def get_position(self, symbol):
        return next((p for p in self.positions if p.symbol == symbol), None)

    async def fetch_user_fills(self, address=None, since_ms=None, *, raise_on_error=False):
        if self.fills_fail:
            if raise_on_error:
                raise ExchangeReadError("fetch_user_fills failed: 429")
            return []
        return list(self.fills)

    async def get_current_price(self, symbol):
        return 100.0

    async def update_leverage(self, *a, **k):
        return True

    async def get_balance(self):
        raise RuntimeError("not needed")

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET):
        self.orders.append((symbol, side, size))
        return Order(
            id=f"order-{len(self.orders)}", symbol=symbol, side=side,
            size=size, order_type=order_type, filled_price=100.0,
            status=OrderStatus.FILLED,
        )


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", MODE)
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_total_exposure_usd", 0)
    monkeypatch.setattr(settings, "dr_freeze_realert_minutes", 15.0)


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = MODE
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


def _runner(redis, exchange, repo=None, strategies=()):
    control = BotControl(redis_url="redis://unused/0", mode=MODE)
    control._redis = redis
    bus = MagicMock()
    bus.publish = AsyncMock(return_value=True)
    runner = EngineRunner(
        exchange=exchange, strategies=list(strategies), repo=repo,
        event_bus=bus, control=control,
    )
    runner._check_parity_after_trade = AsyncMock(return_value=True)
    return runner, bus


def _alerts(bus, strategy="restore-guard"):
    return [
        c.args[0].message for c in bus.publish.await_args_list
        if getattr(c.args[0], "strategy", None) == strategy
    ]


async def _old_trade(repo, oid="111", age=timedelta(hours=3)):
    t = await repo.record_trade(
        order_id=oid, strategy_name="kalman_breakout", symbol="BTC",
        side="buy", size=1.0, price=100.0, reason="signal entry",
    )
    async with repo._session_factory() as session:
        row = await session.get(models.Trade, t.id)
        row.timestamp = NOW - age
        await session.commit()


def _fill(oid, at=NOW):
    return {
        "coin": "BTC", "side": "B", "sz": "1.0", "px": "100.0",
        "fee": "0.01", "time": int(at.timestamp() * 1000), "oid": oid,
    }


class _Strategy:
    """Only the attributes the runner reads outside `_run_strategy`."""

    name = "kalman_breakout"
    symbol = "BTC"
    timeframe = "1h"
    leverage = 1


# ----------------------------------------------------------------------
# Sentinel present: nothing happens
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_present_sentinel_changes_nothing(repo):
    redis = FlakyRedis({SENTINEL: "1"})
    runner, bus = _runner(redis, FakeExchange(), repo)

    await runner.startup()

    assert KILL not in redis.data
    assert PAUSED not in redis.data
    assert _alerts(bus) == []


# ----------------------------------------------------------------------
# Missing sentinel: kill switch, exits keep running
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_sentinel_sets_kill_switch_and_lists_positions(repo):
    await repo.open_position(
        strategy_name="kalman_breakout", symbol="BTC", side="long",
        size=0.5, entry_price=100.0,
    )
    ex = FakeExchange([Position(symbol="BTC", side="long", size=0.5, entry_price=100.0)])
    redis = FlakyRedis()  # the flushed / recreated Redis
    runner, bus = _runner(redis, ex, repo)

    await runner.startup()

    assert redis.data[KILL] == "1"
    assert SENTINEL in redis.data, "written once the bot made itself safe"
    assert redis.data.get(PAUSED) != "1", "kill switch, not pause (decision 5.4)"
    alerts = _alerts(bus)
    assert len(alerts) == 1
    assert "Kill switch is now ON" in alerts[0]
    assert "exchange BTC long 0.5" in alerts[0]
    assert "DB rows kalman_breakout BTC long 0.5" in alerts[0]


@pytest.mark.asyncio
async def test_missing_sentinel_blocks_opens_but_exits_still_run(repo):
    """The point of preferring the kill switch: an SL/TP exit still
    closes, a new entry does not open."""
    await repo.open_position(
        strategy_name="kalman_breakout", symbol="BTC", side="long",
        size=0.5, entry_price=90.0,
    )
    ex = FakeExchange([Position(symbol="BTC", side="long", size=0.5, entry_price=90.0)])
    runner, _ = _runner(FlakyRedis(), ex, repo)
    await runner.startup()

    opened = await runner._execute_signal(
        Signal(action=SignalAction.OPEN_SHORT, symbol="ETH",
               strategy_name="bb_short"),
        current_price=100.0,
    )
    closed = await runner._execute_signal(
        Signal(action=SignalAction.CLOSE_LONG, symbol="BTC",
               strategy_name="kalman_breakout", reason="SL hit"),
        current_price=100.0,
    )

    assert opened is False
    assert closed is True
    assert ex.orders == [("BTC", "sell", 0.5)]
    assert await repo.get_open_positions() == []


@pytest.mark.asyncio
async def test_guard_runs_before_the_startup_reconcile():
    """Its pause (stale case) is what makes that reconcile a dry run, so
    it must come first."""
    order: list[str] = []
    redis = FlakyRedis({SENTINEL: "1"})
    runner, _ = _runner(redis, FakeExchange())
    runner.repo = MagicMock()

    async def check():
        order.append("guard")

    async def reconcile(*_a, **_k):
        order.append("reconcile")
        return ReconcileResult()

    runner._check_control_sentinel = check
    runner.repo.reconcile_positions = AsyncMock(side_effect=reconcile)
    runner.repo.get_open_positions = AsyncMock(return_value=[])

    await runner.startup()

    assert order == ["guard", "reconcile"]


@pytest.mark.asyncio
async def test_redis_lost_under_a_running_bot_is_caught_on_the_next_tick(repo):
    redis = FlakyRedis({SENTINEL: "1"})
    runner, bus = _runner(redis, FakeExchange(), repo)
    await runner.startup()
    assert _alerts(bus) == []

    redis.data.clear()  # redis recreated on an empty volume
    await runner.tick()

    assert redis.data[KILL] == "1"
    assert SENTINEL in redis.data
    assert len(_alerts(bus)) == 1


# ----------------------------------------------------------------------
# Missing sentinel + stale DB: paused, repeated alert
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_sentinel_and_stale_db_pauses_and_dry_runs_reconcile(repo):
    await _old_trade(repo, oid="111")
    # 222 opened a position after the backup the DB was restored from.
    ex = FakeExchange(
        [Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)],
        fills=[_fill(111, NOW - timedelta(hours=3)), _fill(222)],
    )
    redis = FlakyRedis()
    runner, bus = _runner(redis, ex, repo, strategies=[_Strategy()])

    await runner.startup()

    assert redis.data[PAUSED] == "1"
    assert redis.data[KILL] == "1"
    assert FREEZE in redis.data
    assert SENTINEL in redis.data
    assert ex.orders == [], "the startup reconcile ran as a dry run"
    alerts = _alerts(bus)
    assert len(alerts) == 1
    assert "STALE" in alerts[0] and "PAUSED" in alerts[0]
    assert "1 exchange fill(s)" in alerts[0]


@pytest.mark.asyncio
async def test_frozen_bot_repeats_and_escalates_the_alert(repo):
    await _old_trade(repo, oid="111")
    ex = FakeExchange([], fills=[_fill(222)])
    redis = FlakyRedis()
    runner, bus = _runner(redis, ex, repo)
    await runner.startup()
    assert len(_alerts(bus)) == 1

    await runner.tick()
    assert len(_alerts(bus)) == 1, "no reminder before the interval"

    runner._dr_freeze_last_alert -= 15 * 60
    await runner.tick()
    runner._dr_freeze_last_alert -= 15 * 60
    await runner.tick()

    reminders = _alerts(bus)[1:]
    assert [r.split(":")[0] for r in reminders] == ["ESCALATION #1", "ESCALATION #2"]
    assert all("exits and stops are NOT running" in r for r in reminders)


@pytest.mark.asyncio
async def test_resuming_lifts_the_freeze_and_stops_the_reminders(repo):
    await _old_trade(repo, oid="111")
    redis = FlakyRedis()
    runner, bus = _runner(redis, FakeExchange([], fills=[_fill(222)]), repo)
    await runner.startup()

    redis.data[PAUSED] = "0"  # the operator resumes
    await runner.tick()
    runner._dr_freeze_last_alert -= 60 * 60
    await runner.tick()

    assert FREEZE not in redis.data
    assert len(_alerts(bus)) == 1
    assert redis.data[KILL] == "1", "the kill switch stays on until turned off"


@pytest.mark.asyncio
async def test_a_restart_while_frozen_keeps_reminding(repo):
    """The sentinel is back after the first boot, and this restart cannot
    read the exchange's fills, so nothing new is decided at boot; the
    freeze marker carries the reminders over."""
    await _old_trade(repo, oid="111")
    redis = FlakyRedis()
    first, _ = _runner(redis, FakeExchange([], fills=[_fill(222)]), repo)
    await first.startup()

    ex = FakeExchange([], fills=[_fill(222)])
    ex.fills_fail = True
    second, bus = _runner(redis, ex, repo)
    await second.startup()
    await second.tick()

    alerts = _alerts(bus)
    assert len(alerts) == 1 and alerts[0].startswith("ESCALATION #1")
    assert redis.data[PAUSED] == "1"


@pytest.mark.asyncio
async def test_a_restart_while_still_stale_announces_it_again(repo):
    """Every boot checks the DB, sentinel or not: still stale is still
    frozen, and said so."""
    await _old_trade(repo, oid="111")
    redis = FlakyRedis()
    first, _ = _runner(redis, FakeExchange([], fills=[_fill(222)]), repo)
    await first.startup()

    second, bus = _runner(redis, FakeExchange([], fills=[_fill(222)]), repo)
    await second.startup()

    [alert] = _alerts(bus)
    assert alert.startswith("DB looks STALE or RESTORED")
    assert redis.data[PAUSED] == "1" and redis.data[KILL] == "1"


@pytest.mark.asyncio
async def test_fresh_db_does_not_pause(repo):
    """Every fill has a trades row: Redis was lost, the DB was not."""
    await _old_trade(repo, oid="111", age=timedelta(minutes=5))
    redis = FlakyRedis()
    runner, _ = _runner(redis, FakeExchange([], fills=[_fill(111)]), repo)

    await runner.startup()

    assert redis.data[KILL] == "1"
    assert redis.data.get(PAUSED) != "1"


@pytest.mark.asyncio
async def test_unreadable_fills_are_unknown_not_fresh_and_hold_orphans(repo):
    """The HL wrapper answers a failed fills read with []; the guard asks
    for the raising read, so "could not ask" never reads as "no fills".
    Unknown: kill switch only, every orphan held (guard 2 alone would
    close this one: single, known coin, DB 3h old), the check retried
    each tick, and a stale verdict when it comes still pauses."""
    await _old_trade(repo, oid="111")
    ex = FakeExchange(
        [Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)],
        fills=[_fill(111, NOW - timedelta(hours=3)), _fill(222)],
    )
    ex.fills_fail = True
    redis = FlakyRedis()
    runner, bus = _runner(redis, ex, repo, strategies=[_Strategy()])
    runner._run_strategy = AsyncMock()

    await runner.startup()

    assert redis.data[KILL] == "1"
    assert redis.data.get(PAUSED) != "1"
    assert ex.orders == []
    [alert] = _alerts(bus)
    assert "could NOT be checked" in alert
    assert "no unrecorded" not in alert
    [held] = _alerts(bus, "reconcile")
    assert "could not yet be checked" in held

    ex.fills_fail = False  # HL answers again
    await runner.tick()

    assert redis.data[PAUSED] == "1"
    assert "BTC" in runner._held_orphans, "held until a human resolves it"
    assert ex.orders == []
    assert _alerts(bus)[-1].startswith("DB looks STALE or RESTORED")
    runner._run_strategy.assert_not_awaited()


# ----------------------------------------------------------------------
# Fault injection: Redis refuses the guard's writes
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwritable_redis_holds_opens_in_process_and_retries(repo):
    """At maxmemory Redis answers reads and refuses writes: the kill
    switch cannot be saved, and the unset key would read as the env
    default (off). Opens are held in the process instead."""
    redis = FlakyRedis()
    redis.fail_writes = True
    runner, bus = _runner(redis, FakeExchange(), repo)

    await runner.startup()

    assert SENTINEL not in redis.data, "not written until the kill switch is"
    assert await runner.portfolio.check_risk_limits(is_open=True) is False
    assert await runner.portfolio.check_risk_limits(is_open=False) is True
    assert "NOT persisted" in _alerts(bus)[0]

    redis.fail_writes = False
    await runner.tick()

    assert redis.data[KILL] == "1"
    assert SENTINEL in redis.data
    assert runner.portfolio._opens_held is None, "Redis decides again"
    alerts = _alerts(bus)
    assert len(alerts) == 2, "the alert, then one word that it is saved"
    assert alerts[1].startswith("Restore guard complete")

    await runner.tick()
    assert len(_alerts(bus)) == 2, "nothing more once the sentinel is back"


@pytest.mark.asyncio
async def test_unsaved_pause_holds_ticks_and_reconcile_in_process(repo):
    # A single BTC orphan against a 3h-old DB: guard 2 alone would let
    # pass 2 close it, so only the in-process hold keeps reconcile dry.
    await _old_trade(repo, oid="111")
    ex = FakeExchange(
        [Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)],
        fills=[_fill(222)],
    )
    redis = FlakyRedis()
    redis.fail_writes = True
    strat = _Strategy()
    runner, _ = _runner(redis, ex, repo, strategies=[strat])
    runner._run_strategy = AsyncMock()

    await runner.startup()
    await runner.tick()

    assert runner._dr_hold is True
    runner._run_strategy.assert_not_awaited()
    assert ex.orders == []

    redis.fail_writes = False
    await runner.tick()

    assert redis.data[PAUSED] == "1"
    assert runner._dr_hold is False
    runner._run_strategy.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_sentinel_does_nothing_until_it_can_be_read(repo):
    redis = FlakyRedis()
    redis.fail_reads = True
    runner, bus = _runner(redis, FakeExchange(), repo)

    await runner._check_control_sentinel()
    assert redis.data == {}
    assert _alerts(bus) == []

    redis.fail_reads = False
    await runner._check_control_sentinel()
    assert redis.data[KILL] == "1"


# ----------------------------------------------------------------------
# Guard 2 wiring in the runner
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_db_boot_holds_every_exchange_position(repo):
    """The full boot path against an empty DB: the startup reconcile
    alerts on both live positions and orders nothing."""
    ex = FakeExchange([
        Position(symbol="BTC", side="long", size=1.0, entry_price=100.0),
        Position(symbol="ETH", side="short", size=2.0, entry_price=100.0),
    ])
    runner, bus = _runner(FlakyRedis({SENTINEL: "1"}), ex, repo, [_Strategy()])

    await runner.startup()

    assert ex.orders == []
    [notice] = _alerts(bus, "reconcile")
    assert notice.count("HELD exchange-orphan") == 2


@pytest.mark.asyncio
async def test_runner_passes_its_coins_as_the_universe():
    runner, _ = _runner(FlakyRedis({SENTINEL: "1"}), FakeExchange())
    runner.repo = MagicMock()
    runner.repo.reconcile_positions = AsyncMock(return_value=ReconcileResult())
    runner.strategies = [_Strategy()]

    await runner._run_reconcile("Periodic")

    kw = runner.repo.reconcile_positions.await_args.kwargs
    assert kw["strategy_symbols"] == frozenset({"BTC"})


@pytest.mark.asyncio
async def test_held_orphan_is_re_alerted_until_resolved(monkeypatch):
    monkeypatch.setattr(settings, "reconcile_held_orphan_realert_minutes", 60.0)
    held = ReconcileResult(
        failures=["HELD exchange-orphan, NOT closed: long 1.0 ETH ..."],
        held_orphans={"ETH": "reason"},
    )
    runner, bus = _runner(FlakyRedis({SENTINEL: "1"}), FakeExchange())
    runner.repo = MagicMock()
    runner.repo.reconcile_positions = AsyncMock(return_value=held)

    await runner._run_reconcile("Periodic")
    await runner._run_reconcile("Periodic")
    runner._last_reconcile_notice_at -= 60 * 60
    await runner._run_reconcile("Periodic")
    await runner._run_reconcile("Periodic")

    notices = _alerts(bus, "reconcile")
    assert len(notices) == 2
    assert notices[1].startswith("STILL UNRESOLVED")


@pytest.mark.asyncio
async def test_other_repeated_notices_stay_deduplicated():
    """Only held orphans get the reminder; an unpriceable row or an HL
    outage repeating every 5 minutes stays one alert, as before."""
    same = ReconcileResult(failures=["LEFT OPEN (unpriceable): ..."])
    runner, bus = _runner(FlakyRedis({SENTINEL: "1"}), FakeExchange())
    runner.repo = MagicMock()
    runner.repo.reconcile_positions = AsyncMock(return_value=same)

    await runner._run_reconcile("Periodic")
    runner._last_reconcile_notice_at -= 24 * 60 * 60
    await runner._run_reconcile("Periodic")

    assert len(_alerts(bus, "reconcile")) == 1


@pytest.mark.asyncio
async def test_undelivered_reconcile_notice_is_sent_again():
    same = ReconcileResult(failures=["HELD exchange-orphan, NOT closed: ..."])
    runner, bus = _runner(FlakyRedis({SENTINEL: "1"}), FakeExchange())
    bus.publish = AsyncMock(side_effect=[False, True, True])
    runner.repo = MagicMock()
    runner.repo.reconcile_positions = AsyncMock(return_value=same)

    for _ in range(3):
        await runner._run_reconcile("Periodic")

    assert len(_alerts(bus, "reconcile")) == 2, "lost once, sent once"


# ----------------------------------------------------------------------
# A Redis restored from a snapshot keeps the sentinel: the boot DB check
# ----------------------------------------------------------------------


def _btc(side="long", size=1.0):
    return Position(symbol="BTC", side=side, size=size, entry_price=100.0)


@pytest.mark.asyncio
async def test_restored_redis_and_stale_db_pause_before_the_startup_reconcile(repo):
    """PBS restores Redis and the DB together: the sentinel is there, the
    DB is 3h old (inside guard 2's 6h), and the exchange holds a BTC long
    opened after the backup. The startup reconcile used to market-close
    it; the boot check sees fill 222 no trades row records."""
    await _old_trade(repo, oid="111")
    ex = FakeExchange([_btc()], fills=[_fill(111, NOW - timedelta(hours=3)), _fill(222)])
    redis = FlakyRedis({SENTINEL: "1700000000"})
    runner, bus = _runner(redis, ex, repo, strategies=[_Strategy()])

    await runner.startup()

    assert ex.orders == []
    assert redis.data[PAUSED] == "1" and redis.data[KILL] == "1"
    assert FREEZE in redis.data
    [alert] = _alerts(bus)
    assert alert.startswith("DB looks STALE or RESTORED")
    assert "1 exchange fill(s)" in alert and "PAUSED" in alert


@pytest.mark.asyncio
async def test_boot_check_window_survives_rows_written_meanwhile(repo):
    """A check that could not read the fills is retried with the window
    it started with; a trade row written in between would otherwise
    move the window past the unrecorded fill."""
    await _old_trade(repo, oid="111")
    ex = FakeExchange([], fills=[_fill(222, NOW - timedelta(hours=1))])
    ex.fills_fail = True
    runner, _ = _runner(FlakyRedis({SENTINEL: "1"}), ex, repo)

    assert (await runner._assess_db_staleness())[0] is None
    await _old_trade(repo, oid="555", age=timedelta(0))
    ex.fills_fail = False

    assert (await runner._assess_db_staleness())[0] is True


# ----------------------------------------------------------------------
# Guard 2: a held orphan stays held until a human acts
# ----------------------------------------------------------------------


HELD = f"hypertrade:{MODE}:control:held_orphans"


async def _boot_with_held_btc(repo, redis=None, ex=None):
    """Sentinel present, DB 7h old, an untracked BTC short on a coin a
    strategy trades: held for the DB's age."""
    await _old_trade(repo, oid="111", age=timedelta(hours=7))
    ex = ex or FakeExchange([_btc("short")])
    redis = redis if redis is not None else FlakyRedis({SENTINEL: "1"})
    runner, bus = _runner(redis, ex, repo, strategies=[_Strategy()])
    await runner.startup()
    assert "BTC" in runner._held_orphans and ex.orders == []
    return runner, bus, ex, redis


@pytest.mark.asyncio
async def test_opens_on_a_held_coin_are_refused(repo):
    """The DB-based gates cannot see a position with no DB row; HL would
    net this buy into the held short. Other coins still open."""
    runner, _, ex, _ = await _boot_with_held_btc(repo)

    refused = await runner._execute_signal(
        Signal(action=SignalAction.OPEN_LONG, symbol="BTC",
               strategy_name="kalman_breakout"),
        current_price=100.0,
    )
    opened = await runner._execute_signal(
        Signal(action=SignalAction.OPEN_SHORT, symbol="ETH",
               strategy_name="bb_short"),
        current_price=100.0,
    )

    assert refused is False
    assert opened is True
    assert [o[0] for o in ex.orders] == ["ETH"]


@pytest.mark.asyncio
async def test_a_trade_on_another_coin_does_not_release_the_hold(repo):
    """Case (a): the DB looks fresh again once any strategy trades; the
    held BTC short must still wait for a human."""
    runner, _, ex, _ = await _boot_with_held_btc(repo)
    await _old_trade(repo, oid="333", age=timedelta(0))  # a fresh ETH-side row

    await runner._run_reconcile("Periodic")

    assert ex.orders == []
    assert "BTC" in runner._held_orphans


@pytest.mark.asyncio
async def test_closing_one_of_two_held_orphans_keeps_the_other(repo):
    """Case (b): two orphans are held for their count; the operator
    closes one by hand, and the other must not be market-closed for
    being single now."""
    await _old_trade(repo, oid="111", age=timedelta(minutes=5))
    eth = Position(symbol="ETH", side="long", size=2.0, entry_price=100.0)
    ex = FakeExchange([_btc(), eth])
    redis = FlakyRedis({SENTINEL: "1"})
    strategies = [_Strategy(), type("_Eth", (_Strategy,), {"symbol": "ETH"})()]
    runner, _ = _runner(redis, ex, repo, strategies=strategies)
    await runner.startup()
    assert set(runner._held_orphans) == {"BTC", "ETH"}

    ex.positions = [eth]  # BTC closed on the exchange by the operator
    await runner._run_reconcile("Periodic")

    assert ex.orders == []
    assert set(runner._held_orphans) == {"ETH"}
    assert "BTC" not in redis.data[HELD]


@pytest.mark.asyncio
async def test_held_orphans_survive_a_restart(repo):
    """The held set is mirrored to Redis, so a restart after the DB
    turned fresh does not re-decide the coin and close it."""
    _first, _, ex, redis = await _boot_with_held_btc(repo)
    assert "BTC" in redis.data[HELD]
    await _old_trade(repo, oid="333", age=timedelta(0))

    second, _ = _runner(redis, ex, repo, strategies=[_Strategy()])
    await second.startup()

    assert ex.orders == []
    assert "BTC" in second._held_orphans


@pytest.mark.asyncio
async def test_a_held_orphan_closed_by_hand_is_released(repo):
    runner, _, ex, redis = await _boot_with_held_btc(repo)

    ex.positions = []
    await runner._run_reconcile("Periodic")

    assert runner._held_orphans == {}
    assert HELD not in redis.data


# ----------------------------------------------------------------------
# More fault injection
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pause_held_in_process_only_keeps_reminding(repo):
    """Redis refuses writes, so the freeze lives in this process only:
    exits are frozen all the same, so the alert must repeat too."""
    await _old_trade(repo, oid="111")
    redis = FlakyRedis()
    redis.fail_writes = True
    runner, bus = _runner(redis, FakeExchange([], fills=[_fill(222)]), repo)
    await runner.startup()
    assert runner._dr_hold is True

    for _ in range(2):
        runner._dr_freeze_last_alert -= 15 * 60
        await runner.tick()

    reminders = [a for a in _alerts(bus) if a.startswith("ESCALATION")]
    assert [r.split(":")[0] for r in reminders] == ["ESCALATION #1", "ESCALATION #2"]
    assert all("held in this process only" in r for r in reminders)


@pytest.mark.asyncio
async def test_a_failed_kill_switch_write_alone_keeps_the_sentinel_unwritten(repo):
    """Only the kill-switch SET fails (a connection reset): the sentinel
    must not be written over it, or the episode would never be retried
    and the unset key would read as the env default, off."""
    redis = FlakyRedis()
    redis.fail_keys = {KILL}
    runner, bus = _runner(redis, FakeExchange(), repo)

    await runner.startup()

    assert SENTINEL not in redis.data
    assert await runner.portfolio.check_risk_limits(is_open=True) is False

    redis.fail_keys = set()
    await runner.tick()

    assert redis.data[KILL] == "1" and SENTINEL in redis.data
    assert _alerts(bus)[-1].startswith("Restore guard complete")


@pytest.mark.asyncio
async def test_the_episode_completes_when_another_bot_writes_the_sentinel(repo):
    """The sentinel is shared by every bot of the mode. One that writes
    it must not leave this bot frozen in-process, unsaved and silent."""
    await _old_trade(repo, oid="111")
    redis = FlakyRedis()
    redis.fail_writes = True
    runner, bus = _runner(redis, FakeExchange([], fills=[_fill(222)]), repo)
    await runner.startup()
    assert runner._dr_hold is True

    redis.fail_writes = False
    redis.data[SENTINEL] = "1"  # the other bot
    await runner.tick()

    assert runner._dr_hold is False
    assert runner.portfolio._opens_held is None
    assert redis.data[PAUSED] == "1" and redis.data[KILL] == "1"
    assert _alerts(bus)[-1].startswith("Restore guard complete")


@pytest.mark.asyncio
async def test_an_undelivered_guard_alert_is_sent_again(repo):
    """The alert is about Redis and travels over it; the bus returns
    False when Redis did not take it."""
    redis = FlakyRedis()
    runner, bus = _runner(redis, FakeExchange(), repo)
    bus.publish = AsyncMock(side_effect=[False, True, True, True])

    await runner.startup()
    await runner.tick()
    await runner.tick()

    alerts = _alerts(bus)
    assert len(alerts) == 2 and alerts[0] == alerts[1]


@pytest.mark.asyncio
async def test_mainnet_alert_does_not_promise_exits(monkeypatch):
    """On mainnet the tenant's strategy opt-in set lives in the lost
    Redis too, so no strategy runs, exits included (close-only for this
    is NU-5c). The alert must say so."""
    monkeypatch.setattr(settings, "exchange_mode", "mainnet")
    monkeypatch.setattr(settings, "tenant_id", "00000000-0000-0000-0000-000000000001")
    redis = FlakyRedis()
    control = BotControl(redis_url="redis://unused/0", mode="mainnet")
    control._redis = redis
    bus = MagicMock()
    bus.publish = AsyncMock(return_value=True)
    runner = EngineRunner(
        exchange=FakeExchange([_btc()]), strategies=[_Strategy()],
        event_bus=bus, control=control,
    )
    runner._run_strategy = AsyncMock()
    runner._last_hodl_check = float("inf")

    await runner.startup()
    await runner.tick()

    [alert] = _alerts(bus)
    assert "exits do NOT run" in alert
    assert "exits keep running" not in alert
    runner._run_strategy.assert_not_awaited()
