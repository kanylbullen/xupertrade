"""NU-2 guard 2: reconcile pass 2 must not liquidate a restored book.

Pass 2 market-closes every exchange position that has no open DB row.
Against an empty DB, or one restored from an old backup, that is every
live position (roadmap `docs/plans/next-level-roadmap.md` NU-2 point 5).
It now ALERTS instead of closing — an entry in `result.failures` and no
order — when:

- one pass finds more than one such position,
- the coin is outside the bot's strategy universe, or
- the newest trades row (reconcile's own rows not counted) is older
  than `orphan_max_db_age_hours`, or there is none.

A single orphan on a known coin against a live DB is still closed.
Everything here runs against a real Repository on SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.exchange.base import Order, OrderStatus, OrderType, Position

NOW = datetime.now(timezone.utc)
UNIVERSE = frozenset({"BTC", "ETH"})


class FakeExchange:
    def __init__(self, positions, *, fills=None, mid=100.0):
        self.positions = list(positions)
        self.fills = list(fills or [])
        self.mid = mid
        self.orders: list[tuple] = []

    async def get_positions(self):
        return list(self.positions)

    async def fetch_user_fills(self, address=None, since_ms=None):
        return list(self.fills)

    async def get_current_price(self, symbol):
        return self.mid

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET):
        self.orders.append((symbol, side, size))
        return Order(
            id=f"order-{len(self.orders)}", symbol=symbol, side=side,
            size=size, order_type=order_type, filled_price=self.mid,
            status=OrderStatus.FILLED,
        )


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


async def _trade(repo, *, order_id, at, reason="signal entry", strategy="kalman_breakout"):
    trade = await repo.record_trade(
        order_id=order_id, strategy_name=strategy, symbol="SOL",
        side="buy", size=1.0, price=10.0, reason=reason,
    )
    async with repo._session_factory() as session:
        row = await session.get(models.Trade, trade.id)
        row.timestamp = at
        await session.commit()


async def _live_db(repo):
    await _trade(repo, order_id="live-1", at=NOW - timedelta(minutes=10))


async def _trades(repo):
    async with repo._session_factory() as session:
        return list((await session.execute(select(models.Trade))).scalars().all())


def _pos(symbol, side="long", size=1.0):
    return Position(symbol=symbol, side=side, size=size, entry_price=100.0)


async def _reconcile(repo, ex, **kw):
    kw.setdefault("strategy_symbols", UNIVERSE)
    return await repo.reconcile_positions(ex, confirm_delay_seconds=0, **kw)


# ----------------------------------------------------------------------
# The roadmap's "klart när" case
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_db_with_exchange_positions_alerts_and_closes_nothing(repo):
    """A bot booted against an empty (or freshly restored) DB used to
    market-close the whole live book in its first pass."""
    ex = FakeExchange([_pos("BTC"), _pos("ETH", "short", 2.0)])

    result = await _reconcile(repo, ex)

    assert ex.orders == []
    assert result.actions == []
    assert sorted(result.held_orphans) == ["BTC", "ETH"]
    assert len(result.failures) == 2
    assert all(f.startswith("HELD exchange-orphan, NOT closed") for f in result.failures)
    assert all("no trades row" in f for f in result.failures)
    assert result.took_action, "held orphans must reach the runner's publish path"
    assert await _trades(repo) == []


@pytest.mark.asyncio
async def test_empty_db_single_orphan_is_held_too(repo):
    """One position is not a license to close: with no trades row at all
    there is no evidence the DB is live."""
    ex = FakeExchange([_pos("ETH")])

    result = await _reconcile(repo, ex)

    assert ex.orders == []
    assert result.held_orphans == ["ETH"]
    assert "the DB has no trades row for this mode" in result.failures[0]


# ----------------------------------------------------------------------
# Each hold reason on its own, against a live DB
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_orphan_with_fresh_db_is_still_closed(repo):
    await _live_db(repo)
    ex = FakeExchange([_pos("ETH", size=2.0)], mid=2100.0)

    result = await _reconcile(repo, ex)

    assert ex.orders == [("ETH", "sell", 2.0)]
    assert result.held_orphans == []
    assert result.failures == []
    assert any("closed exchange-orphan" in a for a in result.actions)


@pytest.mark.asyncio
async def test_more_than_one_orphan_holds_all_of_them(repo):
    await _live_db(repo)
    ex = FakeExchange([_pos("BTC"), _pos("ETH")])

    result = await _reconcile(repo, ex)

    assert ex.orders == []
    assert sorted(result.held_orphans) == ["BTC", "ETH"]
    assert all("2 exchange positions lack a DB row" in f for f in result.failures)


@pytest.mark.asyncio
async def test_dust_does_not_count_as_an_orphan(repo):
    """Sub-1e-6 rounding leftovers were always ignored; they must not
    push a real single orphan over the "more than one" line."""
    await _live_db(repo)
    ex = FakeExchange([_pos("ETH"), _pos("BTC", size=1e-9)])

    result = await _reconcile(repo, ex)

    assert ex.orders == [("ETH", "sell", 1.0)]
    assert result.held_orphans == []


@pytest.mark.asyncio
async def test_coin_outside_the_universe_is_held(repo):
    """A manual position on a coin no strategy in this bot trades is not
    the bot's to close."""
    await _live_db(repo)
    ex = FakeExchange([_pos("HYPE")])

    result = await _reconcile(repo, ex)

    assert ex.orders == []
    assert result.held_orphans == ["HYPE"]
    assert "no strategy in this bot trades HYPE" in result.failures[0]


@pytest.mark.asyncio
async def test_unknown_universe_holds(repo):
    """A caller that does not say which coins it trades gets the
    conservative answer, not the old close-everything one."""
    await _live_db(repo)
    ex = FakeExchange([_pos("ETH")])

    result = await _reconcile(repo, ex, strategy_symbols=None)

    assert ex.orders == []
    assert "strategy universe is unknown" in result.failures[0]


@pytest.mark.asyncio
async def test_stale_db_holds(repo):
    await _trade(repo, order_id="old-1", at=NOW - timedelta(hours=7))
    ex = FakeExchange([_pos("ETH")])

    result = await _reconcile(repo, ex, orphan_max_db_age_hours=6)

    assert ex.orders == []
    assert "newest trade is older than 6h" in result.failures[0]


@pytest.mark.asyncio
async def test_db_age_threshold_comes_from_settings(repo, monkeypatch):
    from hypertrade.config import settings

    await _trade(repo, order_id="old-1", at=NOW - timedelta(hours=3))
    ex = FakeExchange([_pos("ETH")])

    monkeypatch.setattr(settings, "reconcile_orphan_max_db_age_hours", 2.0)
    held = await _reconcile(repo, ex)
    monkeypatch.setattr(settings, "reconcile_orphan_max_db_age_hours", 4.0)
    closed = await _reconcile(repo, ex)

    assert held.held_orphans == ["ETH"]
    assert closed.held_orphans == [] and len(ex.orders) == 1


@pytest.mark.asyncio
async def test_reconciles_own_trades_do_not_make_the_db_look_live(repo):
    """Pass 1 of a restored DB writes fresh reconcile rows at once; if
    those counted, the same pass's pass 2 would vouch for itself."""
    await _trade(repo, order_id="old-1", at=NOW - timedelta(days=2))
    for i, reason in enumerate((
        "reconcile: orphan close (fill)",
        "ESTIMATED reconcile: orphan close @ mid",
        "reconcile: exchange-orphan close",
    )):
        await _trade(repo, order_id=f"rec-{i}", at=NOW, reason=reason)
    ex = FakeExchange([_pos("ETH")])

    result = await _reconcile(repo, ex)

    assert ex.orders == []
    assert result.held_orphans == ["ETH"]


@pytest.mark.asyncio
async def test_restored_db_pass_one_close_does_not_unlock_pass_two(repo):
    """The whole restore in one pass: a stale DB row whose position is
    gone is closed by pass 1 (writing a fresh reconcile trade), and the
    live ETH position opened after the backup must still be held."""
    await _trade(repo, order_id="old-1", at=NOW - timedelta(days=1))
    await repo.open_position(
        strategy_name="kalman_breakout", symbol="BTC", side="long",
        size=1.0, entry_price=100.0,
    )
    ex = FakeExchange([_pos("ETH")], mid=105.0)

    result = await _reconcile(repo, ex)

    assert any("closed orphan" in a for a in result.actions), "pass 1 still runs"
    assert ex.orders == []
    assert result.held_orphans == ["ETH"]


@pytest.mark.asyncio
async def test_held_message_is_identical_across_passes(repo):
    """The runner publishes a changed summary at once and dedups an
    unchanged one. A live number in the text would re-alert every
    5 minutes."""
    await _trade(repo, order_id="old-1", at=NOW - timedelta(hours=9))
    ex = FakeExchange([_pos("BTC"), _pos("HYPE")])

    first = await _reconcile(repo, ex)
    await _trade(repo, order_id="old-2", at=NOW - timedelta(hours=8))
    second = await _reconcile(repo, ex)

    assert first.summary() == second.summary()


@pytest.mark.asyncio
async def test_paused_pass_still_places_no_orders_and_holds_nothing(repo):
    """Unchanged: a paused (dry-run) pass skips pass 2 entirely."""
    ex = FakeExchange([_pos("BTC"), _pos("ETH")])
    result = await _reconcile(repo, ex, dry_run=True)
    assert ex.orders == []
    assert result.held_orphans == []


# ----------------------------------------------------------------------
# The helpers the guards stand on
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newest_trade_time_is_utc_aware_and_can_skip_reconcile_rows(repo):
    assert await repo.newest_trade_time() is None
    await _trade(repo, order_id="sig", at=NOW - timedelta(hours=5))
    await _trade(
        repo, order_id="rec", at=NOW - timedelta(minutes=1),
        reason="reconcile: exchange-orphan close", strategy="reconcile",
    )

    newest = await repo.newest_trade_time()
    newest_real = await repo.newest_trade_time(exclude_reconcile=True)

    assert newest.tzinfo is not None
    assert abs(newest - (NOW - timedelta(minutes=1))) < timedelta(seconds=1)
    assert abs(newest_real - (NOW - timedelta(hours=5))) < timedelta(seconds=1)


@pytest.mark.asyncio
async def test_newest_trade_time_is_per_mode(repo):
    await _trade(repo, order_id="t1", at=NOW)
    repo._mode = "mainnet"
    assert await repo.newest_trade_time() is None


def _fill(oid, at):
    return {
        "coin": "ETH", "side": "B", "sz": "1.0", "px": "100.0",
        "fee": "0.01", "time": int(at.timestamp() * 1000), "oid": oid,
    }


@pytest.mark.asyncio
async def test_staleness_counts_fills_no_trade_row_records(repo):
    await _trade(repo, order_id="111", at=NOW - timedelta(hours=2))
    ex = FakeExchange([], fills=[
        _fill(111, NOW - timedelta(hours=2)),        # recorded: its own row
        _fill(222, NOW - timedelta(hours=1)),        # never recorded
        _fill(333, NOW - timedelta(minutes=5)),      # never recorded
        _fill(99, NOW - timedelta(hours=3)),         # older than the newest row
    ])

    s = await repo.db_staleness(ex)

    assert s.stale is True
    assert s.unrecorded_fills == 2


@pytest.mark.asyncio
async def test_staleness_false_when_every_fill_has_a_row(repo):
    """Clock skew: the trade row a second older than its own fill is the
    newest row, and its fill must not read as unknown activity."""
    await _trade(repo, order_id="111", at=NOW - timedelta(seconds=2))
    ex = FakeExchange([], fills=[_fill(111, NOW - timedelta(seconds=1))])

    s = await repo.db_staleness(ex)

    assert s.stale is False


@pytest.mark.asyncio
async def test_staleness_on_an_empty_db(repo):
    busy = FakeExchange([], fills=[_fill(1, NOW)])
    quiet = FakeExchange([], fills=[])

    assert (await repo.db_staleness(busy)).stale is True
    assert (await repo.db_staleness(quiet)).stale is False
