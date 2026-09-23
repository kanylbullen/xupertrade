"""Live-mode boot guards (`bot/reports/analysis-2026-09-15.md` § 4).

1. No database on testnet/mainnet → refuse to boot. `main.py` used to set
   `repo = None` and trade anyway, and every guard that reads the DB
   (flip-detect, same-side dedup, coin/family gate, exposure cap, parity)
   silently turned into "allow". Paper keeps the in-memory fallback.

2. The startup open-positions read fails → retry with backoff, then pause
   instead of running strategies flat in memory next to positions the
   exchange still holds. The first un-paused tick retries the restore
   before any strategy runs.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import hypertrade.main as main_mod
from hypertrade.config import settings
from hypertrade.engine.runner import EngineRunner, StartupRefused


class _BrokenRepository:
    def __init__(self, *a, **kw):
        pass

    async def init_db(self):
        raise ConnectionRefusedError("postgres:5432 refused")


# ----------------------------------------------------------------------
# 1. main.py: no DB, no live trading
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["testnet", "mainnet"])
async def test_live_mode_without_db_refuses_to_boot(monkeypatch, mode):
    monkeypatch.setattr(settings, "exchange_mode", mode)
    monkeypatch.setattr(main_mod, "Repository", _BrokenRepository)
    with pytest.raises(StartupRefused):
        await main_mod._connect_repo()


@pytest.mark.asyncio
async def test_paper_without_db_keeps_in_memory_fallback(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "paper")
    monkeypatch.setattr(main_mod, "Repository", _BrokenRepository)
    assert await main_mod._connect_repo() is None


def test_startup_refused_exits_non_zero(monkeypatch):
    """`run()` turns the refusal into exit status 1, so the orchestrator's
    `unless-stopped` policy restarts the container."""
    async def _refuse():
        raise StartupRefused("database unavailable in testnet mode")

    monkeypatch.setattr(main_mod, "main", _refuse)
    monkeypatch.setattr(main_mod.signal, "signal", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as exc_info:
        main_mod.run()
    assert exc_info.value.code == 1


# ----------------------------------------------------------------------
# 2. EngineRunner.startup(): a failed state restore halts trading
# ----------------------------------------------------------------------


class _Strat:
    """Minimal strategy recording what it was restored to."""

    def __init__(self, name="kalman_breakout", symbol="ETH"):
        self.name = name
        self.symbol = symbol
        self.timeframe = "1h"
        self.leverage = 1
        self.restored: list[tuple] = []

    def restore_state(self, side, entry_price):
        self.restored.append((side, entry_price))

    def restore_from_json(self, side, entry_price, state):
        self.restored.append((side, entry_price))


def _row(strategy="kalman_breakout", side="long", entry=2000.0):
    r = MagicMock()
    r.strategy_name = strategy
    r.symbol = "ETH"
    r.side = side
    r.size = 0.5
    r.entry_price = entry
    r.state_json = None
    return r


def _runner(reads, *, control=True):
    """`reads`: per-call answers for repo.get_open_positions — a list of
    rows, or an exception to raise."""
    strat = _Strat()
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(side_effect=list(reads))
    ctl = None
    if control:
        ctl = MagicMock()
        ctl.set_paused = AsyncMock()
        ctl.is_paused = AsyncMock(return_value=False)
        ctl.load_strategy_state = AsyncMock(return_value=None)
    bus = MagicMock()
    bus.publish = AsyncMock()
    runner = EngineRunner(
        exchange=MagicMock(), strategies=[strat], repo=repo,
        event_bus=bus, control=ctl,
    )
    runner._run_reconcile = AsyncMock(return_value=None)
    runner._restore_read_backoff = (0.0, 0.0, 0.0)
    return runner, strat, repo, ctl, bus


def _published(bus):
    return [c.args[0] for c in bus.publish.await_args_list]


@pytest.mark.asyncio
async def test_transient_read_failure_is_retried(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    boom = ConnectionError("db blip")
    runner, strat, repo, ctl, bus = _runner([boom, boom, [_row()]])

    await runner.startup()

    assert repo.get_open_positions.await_count == 3
    assert strat.restored == [("long", 2000.0)]
    assert runner._restore_pending is False
    ctl.set_paused.assert_not_awaited()
    assert _published(bus) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["testnet", "mainnet"])
async def test_persistent_read_failure_pauses_and_alerts(monkeypatch, mode):
    monkeypatch.setattr(settings, "exchange_mode", mode)
    boom = ConnectionError("db down")
    runner, strat, repo, ctl, bus = _runner([boom] * 4)

    await runner.startup()

    # 1 attempt + 3 retries
    assert repo.get_open_positions.await_count == 4
    assert strat.restored == []
    ctl.set_paused.assert_awaited_once_with(True)
    assert runner._restore_pending is True
    events = _published(bus)
    assert len(events) == 1
    assert events[0].strategy == "state-restore"
    assert "PAUSED" in events[0].message


@pytest.mark.asyncio
async def test_paper_read_failure_keeps_running_flat(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "paper")
    boom = ConnectionError("db down")
    runner, strat, repo, ctl, bus = _runner([boom] * 4)

    await runner.startup()

    ctl.set_paused.assert_not_awaited()
    assert runner._restore_pending is False
    assert _published(bus) == []


@pytest.mark.asyncio
async def test_live_read_failure_without_control_refuses_boot(monkeypatch):
    """No BotControl → no pause to set → exit so the container retries."""
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    boom = ConnectionError("db down")
    runner, *_ = _runner([boom] * 4, control=False)

    with pytest.raises(StartupRefused):
        await runner.startup()


def _tick_ready(runner):
    """Stub everything tick() does around the strategy loop."""
    now = time.time()
    runner._last_reconcile = now
    runner._last_funding_poll = now
    runner._last_rate_check = now
    runner.control.beat_heartbeat = AsyncMock()
    runner.control.get_pending_flat_request = AsyncMock(return_value=None)
    runner.control.get_disabled_strategies = AsyncMock(return_value=set())
    runner.control.get_all_leverage_overrides = AsyncMock(return_value={})
    runner._run_strategy = AsyncMock()
    runner._update_position_pnl = AsyncMock()
    runner.exchange.get_balance = AsyncMock(side_effect=RuntimeError("skip"))


@pytest.mark.asyncio
async def test_unpaused_tick_retries_restore_before_strategies(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    boom = ConnectionError("db down")
    runner, strat, repo, ctl, bus = _runner([boom] * 4 + [[_row()]])
    await runner.startup()
    assert runner._restore_pending is True

    # Operator fixed the DB and un-paused.
    _tick_ready(runner)
    await runner.tick()

    assert strat.restored == [("long", 2000.0)]
    assert runner._restore_pending is False
    runner._run_strategy.assert_awaited_once()


@pytest.mark.asyncio
async def test_unpaused_tick_with_db_still_down_repauses(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    boom = ConnectionError("db down")
    runner, strat, repo, ctl, bus = _runner([boom] * 5)
    await runner.startup()
    ctl.set_paused.reset_mock()
    bus.publish.reset_mock()

    _tick_ready(runner)
    await runner.tick()

    runner._run_strategy.assert_not_awaited()
    ctl.set_paused.assert_awaited_once_with(True)
    assert runner._restore_pending is True
    assert [e.strategy for e in _published(bus)] == ["state-restore"]


@pytest.mark.asyncio
async def test_rows_without_a_running_strategy_are_reported(monkeypatch, caplog):
    """An open row whose strategy is not instantiated in this bot
    (allowlist, mainnet opt-in, strategy cap) has nothing managing it.
    It used to be skipped silently."""
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    orphan_a = _row(strategy="bb_short", side="short")
    orphan_b = _row(strategy="moon_phases", side="long")
    runner, strat, repo, ctl, bus = _runner([[_row(), orphan_a, orphan_b]])

    with caplog.at_level("WARNING", logger="hypertrade.engine.runner"):
        await runner.startup()

    assert strat.restored == [("long", 2000.0)]
    unmanaged = [r.message for r in caplog.records if "UNMANAGED" in r.message]
    assert len(unmanaged) == 2
    assert any("bb_short" in m and "ETH" in m and "short" in m for m in unmanaged)
    events = _published(bus)
    assert len(events) == 1
    assert events[0].strategy == "state-restore"
    assert "bb_short ETH short" in events[0].message
    assert "moon_phases ETH long" in events[0].message


@pytest.mark.asyncio
async def test_restore_prefers_state_json(monkeypatch):
    """The extracted per-row restore keeps the startup contract: exact
    state_json when present."""
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    row = _row()
    row.state_json = json.dumps({"entry": 1999.0, "sl": 1900.0, "tp": 2200.0})
    runner, strat, *_ = _runner([[row]])
    strat.restore_from_json = MagicMock()

    await runner.startup()

    strat.restore_from_json.assert_called_once()
    assert strat.restore_from_json.call_args.args[2]["sl"] == 1900.0
