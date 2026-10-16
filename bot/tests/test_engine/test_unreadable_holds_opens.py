"""NU-5b: an unreadable control flag or DB read holds opens, never exits
(roadmap § NU-5 b). The real Repository on SQLite, BotControl on a dict
Redis and the runner's own paths, as in test_reduce_only_closes.py; the
kill switch on a fill the DB missed is decision 5.4 (2026-10-16)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.runner import TradeDbDivergence
from hypertrade.engine.signals import Signal, SignalAction
from hypertrade.exchange.base import Position

from .test_reduce_only_closes import (  # noqa: F401 — `repo`, `_settings`: fixtures
    ENTRY, TENANT, Exit, HLLike, _hold, _runner, _settings, _trades, repo,
)
from .test_restore_guards import FakeRedis


def _long(symbol="ETH", size=1.0):
    return Position(symbol=symbol, side="long", size=size, entry_price=ENTRY)


class Opener(Exit):
    """Asks for `want` every tick it holds anything else: flat, a standing
    entry (sma_rsi); holding the other side, a flip (hash_supertrend)."""

    want = "long"

    async def on_candle(self, candles):
        if self.side == self.want:
            return None
        self.side = self.want
        return Signal(action=getattr(SignalAction, f"OPEN_{self.want.upper()}"),
                      symbol=self.symbol, strategy_name=self.name, reason="entry")


class Flip(Opener):
    want = "short"


class Redis(FakeRedis):
    """Set and hash reads fail, like GET, for keys in `fail_get`."""

    async def smembers(self, key):
        return await self.get(key) or set()

    async def hgetall(self, key):
        return await self.get(key) or {}


@pytest.fixture(autouse=True)
def _quiet_tick(monkeypatch):
    monkeypatch.setattr(settings, "services_owner", False)
    monkeypatch.setattr(settings, "trade_rate_alarm_enabled", False)


async def _ticking(repo, *strategies, positions=()):
    """A runner whose `tick()` has only the control reads, flat-all and the
    strategy loop to do: reconcile and the funding poll are not due."""
    ex = HLLike(*positions)
    runner, bus = _runner(repo, ex, *strategies)
    runner.control._redis = Redis()
    await runner.control.write_sentinel()
    runner._last_reconcile = runner._last_funding_poll = time.time()
    return ex, runner, bus


@pytest.mark.parametrize("flag", [
    "flat-all request", "paused flag", "disabled set", "leverage overrides",
    "disabled set and rows", "mainnet allowlist",
])
async def test_an_unreadable_control_flag_holds_opens_never_exits(
    repo, monkeypatch, flag,
):
    """exits-never-blocked-4: a failed read raised out of `tick()`, an
    unreadable allowlist ran nothing. Now no opens, the stop closes, and an
    unreadable disabled set or allowlist runs only the holders."""
    if flag == "mainnet allowlist":
        monkeypatch.setattr(settings, "exchange_mode", "mainnet")
    held, opener = Exit("ETH"), Opener("BTC")
    ex, runner, bus = await _ticking(repo, held, opener, positions=[_long()])
    await _hold(repo, held)
    ctl = runner.control
    ctl._redis.fail_get.add({
        "flat-all request": ctl._key_flat_req, "paused flag": ctl._key_paused,
        "leverage overrides": ctl._key_leverage,
        "mainnet allowlist": ctl._mainnet_enabled_strategies_key(TENANT),
    }.get(flag, ctl._key_disabled))
    if flag.endswith("rows"):
        repo.get_open_positions = AsyncMock(side_effect=ConnectionError("db"))
    opener.on_candle = AsyncMock(wraps=opener.on_candle)

    await runner.tick()

    assert [(o["symbol"], o["side"], o["reduce_only"]) for o in ex.orders] == [
        ("ETH", "sell", True),
    ], "the exit, and no open"
    assert ex.book == {} and held.side is None and opener.side is None
    holders_only = flag.startswith(("disabled", "mainnet"))
    assert opener.on_candle.await_count == (0 if holders_only else 1)


async def test_a_flat_all_that_cannot_finish_holds_opens_and_says_so_once(repo):
    """flip-and-flat-2, exits-never-blocked-6, r1-tests-and-noise-2: BTC's
    close keeps failing; ETH's entry re-opened what flat-all closed, and the
    notice went every tick as its reason changed ("1 of 2", "1 of 1")."""
    eth, btc = Opener("ETH"), Exit("BTC")
    btc.quiet = True
    ex, runner, bus = await _ticking(
        repo, eth, btc, positions=[_long(), _long("BTC")],
    )
    await _hold(repo, eth)
    await _hold(repo, btc)
    real, refusing = ex.place_order, [True]

    async def refuse_btc(symbol, *a, **kw):
        ex.refuse = int(symbol == "BTC" and refusing[0])
        return await real(symbol, *a, **kw)

    ex.place_order = refuse_btc
    await runner.control.request_flat_all("tok-1")
    for _ in range(3):
        await runner.tick()

    assert [o["side"] for o in ex.orders if o["symbol"] == "ETH"] == ["sell"]
    assert "ETH" not in ex.book and eth.side is None
    [notice] = bus.errors("flat-all")
    assert notice.startswith("Flat-all did not complete: 1 of 2 close(s) failed")
    assert not await runner.control.is_paused()

    refusing[0] = False
    await runner.tick()
    assert ex.book == {} and await runner.control.is_paused()


@pytest.mark.parametrize(
    "exchange", ["holds it", "refuses it", "flat", "unreadable", "shared"],
)
async def test_an_exit_whose_row_is_unreadable_closes_the_exchange_side(
    repo, exchange,
):
    """NU-5b's DB half: the exit was dropped. Now the exchange's side goes
    out reduce-only; with nothing safe to send (flat, unreadable, another
    holder on the coin HL nets) it raises. Unfilled, the strategy re-arms."""
    strat, other = Exit(), Exit()
    other.name, other.side, other.quiet = "t_ETH", "long", True
    ex, runner, bus = await _ticking(
        repo, strat, *[other][:exchange == "shared"],
        positions=[] if exchange == "flat" else [_long()],
    )
    await _hold(repo, strat)
    ex.read_errors = int(exchange == "unreadable")
    ex.refuse = int(exchange == "refuses it")
    real, blips = repo.get_open_position, [ConnectionError("db blip")]

    async def blip(*a):
        if blips:
            raise blips.pop()
        return await real(*a)

    repo.get_open_position = blip
    sent, booked = exchange in ("holds it", "refuses it"), exchange == "holds it"
    if sent:
        await runner._run_strategy(strat)
    else:
        with pytest.raises(ConnectionError):
            await runner._run_strategy(strat)

    assert ex.orders == [{
        "symbol": "ETH", "side": "sell", "size": 1.0, "reduce_only": True,
        "slippage": None,
    }][:sent]
    assert [(t.size, t.reason) for t in await _trades(repo)] == [
        (1.0, "stop hit"),
    ][:booked]
    assert strat.side == (None if booked else "long")


@pytest.mark.parametrize("fault", ["close order", "flip row read", "open row read"])
async def test_an_exit_that_raises_leaves_the_strategy_holding(repo, fault):
    """exits-never-blocked-2: raised before a fill, the strategy takes back
    what it dropped on the signal (a plain open ends flat)."""
    strat = {"close order": Exit, "flip row read": Flip, "open row read": Opener}[fault]()
    ex, runner, bus = await _ticking(repo, strat, positions=[_long()])
    if fault != "open row read":
        await _hold(repo, strat)
    if fault == "close order":
        ex.place_order = AsyncMock(side_effect=RuntimeError("SDK bug"))
    else:
        repo.get_open_position = AsyncMock(side_effect=ConnectionError("db blip"))

    with pytest.raises((RuntimeError, ConnectionError)):
        await runner._run_strategy(strat)

    assert strat.side == (None if fault == "open row read" else "long")
    assert ex.orders == []


async def test_a_db_outage_still_sends_every_exit(repo):
    """"Ett Redis- eller DB-fel inte stoppar SL-exit". DB down: ETH's stop
    (tick 1) and BTC's (tick 3) go out reduce-only at the exchange's size.
    ETH's fill turns the kill switch on (its failed write holds opens and is
    retried), not the pause that froze BTC's exit. Nothing is re-armed, and
    reconcile pass 1 books both rows from their own fills afterwards."""
    eth, btc = Exit("ETH"), Exit("BTC")
    btc.quiet = True
    ex, runner, bus = await _ticking(
        repo, eth, btc, positions=[_long(), _long("BTC", 0.5)],
    )
    await _hold(repo, eth)
    await _hold(repo, btc, size=0.5)
    real, ctl, up = ex.place_order, runner.control, repo._session_factory

    async def place(symbol, side, size, *a, **kw):  # HL's fill history
        order = await real(symbol, side, size, *a, **kw)
        ex.fills.append({
            "coin": symbol, "side": "A", "sz": str(order.size), "fee": "0.1",
            "px": str(order.filled_price), "oid": order.id,
            "time": int(time.time() * 1000) + 60_000,
        })
        return order

    def down():
        raise ConnectionError("db down")

    ex.place_order, repo._session_factory = place, down
    ctl._redis.fail_set.add(ctl._key_kill_switch)
    await runner.tick()
    assert not await runner.portfolio.check_risk_limits(is_open=True), "held"
    ctl._redis.fail_set.clear()
    await runner.tick()
    assert await ctl.is_kill_switch_active() and not await ctl.is_paused()
    btc.quiet = False
    await runner.tick()

    assert [(o["symbol"], o["size"], o["reduce_only"]) for o in ex.orders] == [
        ("ETH", 1.0, True), ("BTC", 0.5, True),
    ]
    assert ex.book == {} and eth.side is None and btc.side is None
    [alert] = bus.errors(eth.name)
    assert "Kill switch ON: no new opens, exits keep running" in alert

    repo._session_factory = up
    await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, hold_orphans=None,
        traded_symbols={"ETH", "BTC"},
    )
    assert await repo.get_open_positions() == []
    assert sorted((t.order_id, t.size) for t in await _trades(repo)) == sorted(
        (f["oid"], float(f["sz"])) for f in ex.fills
    )


@pytest.mark.parametrize("close", ["short fill", "flip"])
async def test_a_fill_the_db_missed_re_arms_nothing(repo, close):
    """failure-injection-3: a close fills 0.4 of 1.0 (or a flip's close
    fills) and its DB write fails. Nothing re-arms it, a flip opens nothing,
    the kill switch is on; the restart the alert asks for manages the 0.6."""
    strat = Flip() if close == "flip" else Exit()
    ex, runner, bus = await _ticking(repo, strat, positions=[_long()])
    await _hold(repo, strat)
    real = ex.place_order
    if close == "short fill":
        ex.place_order = lambda s, d, size, *a, **kw: real(s, d, min(size, 0.4), *a, **kw)
    repo.record_trade_and_close_position = AsyncMock(side_effect=ConnectionError("db"))

    with pytest.raises(TradeDbDivergence):
        await runner._run_strategy(strat)

    assert [(o["side"], o["reduce_only"]) for o in ex.orders] == [("sell", True)]
    assert strat.side is None and (await repo.get_open_positions())[0].size == 1.0
    assert await runner.control.is_kill_switch_active()
    assert not await runner.control.is_paused()
    if close == "flip":
        return
    [alert] = bus.errors(strat.name)
    assert "0.6 stays open on its row at the old size" in alert
    del repo.record_trade_and_close_position
    ex.place_order = real
    assert await runner._restore_from_db(())
    await runner._run_strategy(strat)
    assert ex.book == {} and await repo.get_open_positions() == []
