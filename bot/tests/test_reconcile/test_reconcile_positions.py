"""Reconcile must never invent a close.

Every case here is a live failure from `bot/reports/analysis-2026-09-15.md`
§ 2: `HyperLiquidExchange.get_positions()` answered a 502 with `[]`,
reconcile read that as "the exchange is flat", and closed every open DB
row at `exit_price = entry_price`, `pnl = 0.0`, with no `Trade` row —
31 % of all testnet closes since May. Five minutes later pass 2
market-closed the still-real exchange positions and counted a REJECTED
order as a cleanup.

The invariants under test:

1. A failed read writes nothing and orders nothing.
2. "Exchange is flat" is confirmed by a second read before any close.
3. A close that does happen is priced from the exchange and bookkept
   with a `Trade` row; if it cannot be priced, the row stays OPEN.
4. A rejected exchange-orphan close is a failure, not an action.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.exchange.base import (
    ExchangeReadError,
    Order,
    OrderStatus,
    OrderType,
    Position,
)

NOW = datetime.now(timezone.utc)
OPENED_AT = NOW - timedelta(hours=1)
FILL_MS = int((NOW - timedelta(minutes=5)).timestamp() * 1000)


class FakeExchange:
    """Scriptable stand-in for the HL wrapper.

    `position_reads` is consumed one entry per `get_positions()` call —
    a list of Positions to return, or an exception instance to raise.
    The last entry repeats once exhausted.
    """

    def __init__(
        self,
        position_reads,
        *,
        fills=None,
        mid=0.0,
        order_status=OrderStatus.FILLED,
        order_raises=None,
    ):
        self._reads = list(position_reads)
        self.read_count = 0
        self.fills = list(fills or [])
        self.fills_calls: list[int | None] = []
        self.mid = mid
        self.orders: list[tuple] = []
        self._order_status = order_status
        self._order_raises = order_raises

    async def get_positions(self):
        self.read_count += 1
        idx = min(self.read_count - 1, len(self._reads) - 1)
        answer = self._reads[idx]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)

    async def fetch_user_fills(self, address=None, since_ms=None):
        self.fills_calls.append(since_ms)
        return list(self.fills)

    async def get_current_price(self, symbol):
        return self.mid

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET):
        self.orders.append((symbol, side, size, order_type))
        if self._order_raises is not None:
            raise self._order_raises
        return Order(
            id=f"order-{len(self.orders)}",
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            filled_price=self.mid or 100.0,
            status=self._order_status,
        )


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:")
    r._mode = "testnet"
    r._is_paper = False
    await r.init_db()
    yield r
    await r._engine.dispose()


async def _open_row(
    repo, *, strategy="kalman_breakout", symbol="BTC", side="long",
    size=1.0, entry=100.0,
):
    pos = await repo.open_position(
        strategy_name=strategy, symbol=symbol, side=side,
        size=size, entry_price=entry,
    )
    async with repo._session_factory() as session:
        row = await session.get(models.PositionRecord, pos.id)
        row.opened_at = OPENED_AT
        await session.commit()
    return pos


async def _rows(repo, model):
    async with repo._session_factory() as session:
        result = await session.execute(select(model))
        return list(result.scalars().all())


def _sell_fill(**over):
    fill = {
        "coin": "BTC", "side": "A", "sz": "1.0", "px": "110.0",
        "fee": "0.5", "time": FILL_MS, "oid": 991122,
    }
    fill.update(over)
    return fill


# Pass 2 only market-closes an exchange orphan the orphan guard (NU-2)
# lets through: one orphan in the pass, on a coin a strategy in the bot
# trades, against a DB whose newest trade is recent. These pass-2 tests
# are about what happens once a close is allowed, so they set that up.
UNIVERSE = frozenset({"BTC", "ETH"})


async def _live_db(repo):
    """A recent signal-path trade: the DB is live, not restored."""
    await repo.record_trade(
        order_id="live-1", strategy_name="kalman_breakout", symbol="SOL",
        side="buy", size=1.0, price=10.0, reason="signal entry",
    )


# ----------------------------------------------------------------------
# 1. A failed read is not "flat"
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_failure_changes_nothing(repo):
    """The whole bug in one test: HL 502s, and reconcile must not touch
    a single row or place a single order."""
    await _open_row(repo)
    ex = FakeExchange([ExchangeReadError("get_positions failed: 502 Bad Gateway")])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert result.skipped is not None
    assert "read failed" in result.skipped
    assert result.actions == []
    assert ex.orders == []
    positions = await _rows(repo, models.PositionRecord)
    assert [p.is_open for p in positions] == [True]
    assert positions[0].pnl is None
    assert await _rows(repo, models.Trade) == []


@pytest.mark.asyncio
async def test_read_failure_marker_reaches_the_caller(repo):
    """`skipped` is the thing the old `list[str]` return could not say:
    an empty action list meant both 'clean' and 'never asked'."""
    ex = FakeExchange([ExchangeReadError("boom")])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)
    assert result.summary().startswith("skipped:")
    assert result.took_action is False


@pytest.mark.asyncio
async def test_non_read_error_also_skips(repo):
    """A PaperExchange or a mock raising a plain Exception gets the same
    protection — the guard is not keyed on the exception type."""
    await _open_row(repo)
    ex = FakeExchange([RuntimeError("something else")])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert result.skipped is not None
    assert (await _rows(repo, models.PositionRecord))[0].is_open is True


# ----------------------------------------------------------------------
# 2. "Exchange is flat" gets a second look
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flat_exchange_triggers_confirming_reread(repo):
    """One empty read is not enough to close the book."""
    await _open_row(repo)
    ex = FakeExchange([[], []], fills=[_sell_fill()])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert ex.read_count == 2, "no confirming re-read happened"
    assert len(result.actions) == 1


@pytest.mark.asyncio
async def test_confirming_reread_finding_positions_cancels_the_close(repo):
    """The exact 502 shape: first read empty, second read shows the
    position was there all along. Nothing may be closed."""
    await _open_row(repo)
    ex = FakeExchange([
        [],
        [Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)],
    ])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert ex.read_count == 2
    assert result.actions == []
    assert (await _rows(repo, models.PositionRecord))[0].is_open is True


@pytest.mark.asyncio
async def test_confirming_reread_raising_skips(repo):
    """If we cannot confirm, we do not act."""
    await _open_row(repo)
    ex = FakeExchange([[], ExchangeReadError("502 on the confirming read")])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert ex.read_count == 2
    assert result.skipped is not None
    assert "confirming" in result.skipped
    assert (await _rows(repo, models.PositionRecord))[0].is_open is True


@pytest.mark.asyncio
async def test_no_reread_when_db_is_empty(repo):
    """Nothing at stake, no second round-trip."""
    ex = FakeExchange([[]])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)
    assert ex.read_count == 1
    assert result.summary() == "clean"


@pytest.mark.asyncio
async def test_confirm_delay_defaults_to_a_real_pause():
    """The default must not be 0 — the re-read only helps if HL has had
    a moment to recover."""
    import inspect

    sig = inspect.signature(Repository.reconcile_positions)
    assert sig.parameters["confirm_delay_seconds"].default >= 1.0


# ----------------------------------------------------------------------
# 3. A close that happens is priced and bookkept
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_close_priced_from_fill_writes_trade(repo):
    """Long 1.0 BTC from 100, closed by a sell fill at 110 with a 0.5
    fee. exit_price = 110, pnl = +9.5, and a Trade row exists carrying
    the fill's oid."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill()])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert len(result.actions) == 1
    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.is_open is False
    assert pos.exit_price == pytest.approx(110.0)
    assert pos.pnl == pytest.approx(9.5)

    trades = await _rows(repo, models.Trade)
    assert len(trades) == 1
    assert trades[0].order_id == "991122"
    assert trades[0].side == "sell"
    assert trades[0].price == pytest.approx(110.0)
    assert trades[0].fee == pytest.approx(0.5)
    assert trades[0].pnl == pytest.approx(9.5)
    assert trades[0].reason == "reconcile: orphan close (fill)"
    assert trades[0].mode == "testnet"
    # The fill window starts at the row's opened_at, not "recent".
    assert ex.fills_calls == [int(OPENED_AT.timestamp() * 1000)]


@pytest.mark.asyncio
async def test_orphan_close_pnl_is_side_aware(repo):
    """A short closed ABOVE its entry is a LOSS. The pre-fix code wrote
    0.0 for both directions, which is how -$40 of real losses never
    reached /eval or the daily-loss counter."""
    await _open_row(repo, side="short", size=1.0, entry=100.0)
    # A short is closed by a BUY.
    ex = FakeExchange(
        [[], []],
        fills=[_sell_fill(side="B", px="110.0", fee="0.5")],
    )

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert len(result.actions) == 1
    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.pnl == pytest.approx(-10.5)  # (100 - 110) * 1 - 0.5


@pytest.mark.asyncio
async def test_sell_fill_does_not_price_a_short_close(repo):
    """Side matching must not be sloppy: a SELL on the same coin is the
    OPEN of a short, not its close. Falls through to the mid price."""
    await _open_row(repo, side="short", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill()], mid=105.0)

    await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    trades = await _rows(repo, models.Trade)
    assert trades[0].reason == "ESTIMATED reconcile: orphan close @ mid"


@pytest.mark.asyncio
async def test_fills_before_opened_at_are_ignored(repo):
    """A previous position's closing fill on the same coin must not be
    used to price this one."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    stale = _sell_fill(
        px="500.0", time=int((OPENED_AT - timedelta(hours=2)).timestamp() * 1000),
    )
    ex = FakeExchange([[], []], fills=[stale], mid=105.0)

    await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.exit_price == pytest.approx(105.0)


@pytest.mark.asyncio
async def test_orphan_close_falls_back_to_mid(repo):
    """No fill found (HL's fill window rolled over, say) — price from the
    mid and SAY it is an estimate."""
    from hypertrade.config import settings

    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[], mid=105.0)

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert len(result.actions) == 1
    assert "ESTIMATED" in result.actions[0]
    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.exit_price == pytest.approx(105.0)
    expected_fee = 105.0 * 1.0 * settings.taker_fee_rate
    assert pos.pnl == pytest.approx(5.0 - expected_fee)

    trades = await _rows(repo, models.Trade)
    assert trades[0].reason == "ESTIMATED reconcile: orphan close @ mid"
    assert trades[0].order_id.startswith("reconcile-")


@pytest.mark.asyncio
async def test_unpriceable_row_stays_open(repo):
    """No fill AND no mid price. The old code would have written
    pnl=0.0. Leaving it open and shouting is the honest answer."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[], mid=0.0)

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert result.actions == []
    assert len(result.failures) == 1
    assert "LEFT OPEN" in result.failures[0]
    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.is_open is True
    assert pos.pnl is None
    assert await _rows(repo, models.Trade) == []


@pytest.mark.asyncio
async def test_wrong_side_close_is_priced_too(repo):
    """DB says long, exchange says short. Same bookkeeping as an orphan."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex = FakeExchange(
        [[Position(symbol="BTC", side="short", size=1.0, entry_price=108.0)]],
        fills=[_sell_fill()],
    )

    result = await repo.reconcile_positions(
        ex, close_exchange_orphans=False, confirm_delay_seconds=0,
    )

    assert len(result.actions) == 1
    assert "wrong-side" in result.actions[0]
    trades = await _rows(repo, models.Trade)
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(9.5)


@pytest.mark.asyncio
async def test_two_rows_sharing_one_fill_both_get_a_trade_row(repo):
    """One 2.0 fill genuinely closing two 1.0 rows (HL nets per coin) is
    the legitimate shared-fill case, as opposed to the double-booking in
    `test_a_fill_prices_one_row_and_the_next_falls_back`: the ledger has
    the size for both, so both price from the fill and the fee is SPLIT
    between them, not charged twice.

    `Trade.order_id` is UNIQUE, so the second row falls back to a
    synthetic id instead of taking the whole transaction down with an
    IntegrityError."""
    await _open_row(repo, strategy="strat_a", side="long", size=1.0, entry=100.0)
    await _open_row(repo, strategy="strat_b", side="long", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill(sz="2.0", fee="0.5")])

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert len(result.actions) == 2
    trades = await _rows(repo, models.Trade)
    assert len(trades) == 2
    order_ids = sorted(t.order_id for t in trades)
    assert order_ids[0] == "991122"
    assert order_ids[1].startswith("reconcile-")
    # Both fill-priced, and the fill's 0.5 fee split 0.25/0.25.
    assert all(t.reason == "reconcile: orphan close (fill)" for t in trades)
    assert all(t.fee == pytest.approx(0.25) for t in trades)
    assert sum(t.fee for t in trades) == pytest.approx(0.5)
    positions = await _rows(repo, models.PositionRecord)
    assert all(p.is_open is False for p in positions)


@pytest.mark.asyncio
async def test_on_strategy_close_receives_the_realised_pnl(repo):
    """The callback is how a reconcile close reaches
    `portfolio.record_pnl` and therefore the daily-loss kill switch."""
    await _open_row(repo, strategy="hash_momentum", side="long", entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill()])
    seen: list[tuple] = []

    async def cb(name, pnl):
        seen.append((name, pnl))

    await repo.reconcile_positions(
        ex, on_strategy_close=cb, confirm_delay_seconds=0,
    )

    assert len(seen) == 1
    assert seen[0][0] == "hash_momentum"
    assert seen[0][1] == pytest.approx(9.5)


@pytest.mark.asyncio
async def test_on_strategy_close_not_called_for_unpriceable_rows(repo):
    """The row stayed open, so the strategy's in-memory state must stay
    put too."""
    await _open_row(repo, side="long", entry=100.0)
    ex = FakeExchange([[], []], fills=[], mid=0.0)
    seen: list[tuple] = []

    async def cb(name, pnl):
        seen.append((name, pnl))

    await repo.reconcile_positions(
        ex, on_strategy_close=cb, confirm_delay_seconds=0,
    )
    assert seen == []


# ----------------------------------------------------------------------
# 4. Pass 2 — exchange orphans
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_orphan_filled_is_an_action_with_a_trade_row(repo):
    """Single orphan, known coin, live DB: still closed, as before NU-2."""
    await _live_db(repo)
    ex = FakeExchange(
        [[Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)]],
        mid=2100.0,
    )

    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, strategy_symbols=UNIVERSE,
    )

    assert len(result.actions) == 1
    assert "exchange-orphan" in result.actions[0]
    assert result.failures == []
    assert result.held_orphans == {}
    assert ex.orders == [("ETH", "sell", 2.0, OrderType.MARKET)]

    trades = [
        t for t in await _rows(repo, models.Trade) if t.order_id != "live-1"
    ]
    assert len(trades) == 1
    assert trades[0].reason == "reconcile: exchange-orphan close"
    assert trades[0].strategy_name == "reconcile"
    assert trades[0].price == pytest.approx(2100.0)
    # No DB row means no entry price, so no honest PnL — NULL, not 0.0.
    assert trades[0].pnl is None


@pytest.mark.asyncio
async def test_rejected_exchange_orphan_is_a_failure_not_an_action(repo):
    """Seen twice live: a REJECTED close was reported as a cleanup while
    the position was still sitting on the exchange."""
    await _live_db(repo)
    ex = FakeExchange(
        [[Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)]],
        mid=2100.0,
        order_status=OrderStatus.REJECTED,
    )

    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, strategy_symbols=UNIVERSE,
    )

    assert result.actions == []
    assert len(result.failures) == 1
    assert "rejected" in result.failures[0].lower()
    assert [t.order_id for t in await _rows(repo, models.Trade)] == ["live-1"]


@pytest.mark.asyncio
async def test_place_order_raising_is_a_failure(repo):
    await _live_db(repo)
    ex = FakeExchange(
        [[Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)]],
        order_raises=RuntimeError("HL rejected the close"),
    )

    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, strategy_symbols=UNIVERSE,
    )

    assert ex.orders, "the close was attempted"
    assert result.actions == []
    assert len(result.failures) == 1
    assert result.failures[0].startswith("FAILED to close")


@pytest.mark.asyncio
async def test_close_exchange_orphans_false_places_no_orders(repo):
    """The runner passes this while the bot is paused."""
    ex = FakeExchange(
        [[Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)]],
        mid=2100.0,
    )

    result = await repo.reconcile_positions(
        ex, close_exchange_orphans=False, confirm_delay_seconds=0,
    )

    assert ex.orders == []
    assert result.actions == []


# ----------------------------------------------------------------------
# 5. A fill belongs to exactly one close (review items 1 and 2)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_already_booked_as_a_trade_is_not_reused(repo):
    """The sharp edge: daily_long_0830's 08:15 sell on BTC is already a
    `trades` row from its own signal path. Handing it to an orphaned
    hash_supertrend row as an authoritative "(fill)" price would book
    the same money twice, to two strategies."""
    await repo.record_trade(
        order_id="991122",
        strategy_name="daily_long_0830",
        symbol="BTC",
        side="sell",
        size=1.0,
        price=110.0,
        fee=0.5,
        pnl=9.5,
        reason="signal exit",
    )
    await _open_row(repo, strategy="hash_supertrend", side="long", entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill()], mid=105.0)

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert len(result.actions) == 1
    assert "ESTIMATED" in result.actions[0]
    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.exit_price == pytest.approx(105.0)  # mid, not the fill's 110

    trades = await _rows(repo, models.Trade)
    assert len(trades) == 2
    orphan_trade = next(t for t in trades if t.strategy_name == "hash_supertrend")
    assert orphan_trade.order_id != "991122"
    assert orphan_trade.reason.startswith("ESTIMATED")


@pytest.mark.asyncio
async def test_a_fill_prices_one_row_and_the_next_falls_back(repo):
    """Two rows, one fill big enough for exactly one of them. The fill's
    fee must be booked once, not twice."""
    await _open_row(repo, strategy="strat_a", side="long", size=0.0025, entry=100.0)
    await _open_row(repo, strategy="strat_b", side="long", size=0.0025, entry=100.0)
    ex = FakeExchange(
        [[], []], fills=[_sell_fill(sz="0.0025", px="110.0", fee="0.5")], mid=105.0,
    )

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert len(result.actions) == 2
    trades = await _rows(repo, models.Trade)
    assert len(trades) == 2
    by_reason = {t.reason: t for t in trades}
    fill_trade = by_reason["reconcile: orphan close (fill)"]
    est_trade = by_reason["ESTIMATED reconcile: orphan close @ mid"]
    assert fill_trade.price == pytest.approx(110.0)
    assert fill_trade.fee == pytest.approx(0.5)
    assert est_trade.price == pytest.approx(105.0)
    # The 0.5 fill fee is charged once; the estimate carries only the
    # configured taker rate on its own notional.
    assert est_trade.fee < 0.01
    # And no savepoint fallback was needed, because only one row could
    # claim the oid.
    assert fill_trade.order_id == "991122"
    assert est_trade.order_id.startswith("reconcile-")


@pytest.mark.asyncio
async def test_a_fill_too_small_for_the_row_prices_at_mid(repo):
    """Partial coverage is not a close price — full fill coverage or
    the mid, never a blend."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill(sz="0.4")], mid=105.0)

    await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    pos = (await _rows(repo, models.PositionRecord))[0]
    assert pos.exit_price == pytest.approx(105.0)


# ----------------------------------------------------------------------
# 6. The confirming re-read covers partial blips (review item 3)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_read_gap_triggers_the_reread(repo):
    """The exchange answers for ETH but drops BTC on the first read, and
    returns both on the second. Confirming only the all-empty case let
    exactly this close a row."""
    await _open_row(repo, strategy="strat_eth", symbol="ETH", side="long", size=2.0)
    await _open_row(repo, strategy="strat_btc", symbol="BTC", side="long", size=1.0)
    both = [
        Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0),
        Position(symbol="BTC", side="long", size=1.0, entry_price=100.0),
    ]
    ex = FakeExchange([[both[0]], both], fills=[_sell_fill()])

    result = await repo.reconcile_positions(
        ex, close_exchange_orphans=False, confirm_delay_seconds=0,
    )

    assert ex.read_count == 2
    assert result.actions == []
    assert all(p.is_open for p in await _rows(repo, models.PositionRecord))


@pytest.mark.asyncio
async def test_wrong_side_alone_triggers_the_reread(repo):
    """A side flip on one coin is a close candidate too."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    flipped = [Position(symbol="BTC", side="short", size=1.0, entry_price=100.0)]
    correct = [Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)]
    ex = FakeExchange([flipped, correct])

    result = await repo.reconcile_positions(
        ex, close_exchange_orphans=False, confirm_delay_seconds=0,
    )

    assert ex.read_count == 2
    assert result.actions == []


@pytest.mark.asyncio
async def test_no_reread_when_nothing_is_closeable(repo):
    """The book agrees with the DB — no second round-trip."""
    await _open_row(repo, side="long", size=1.0)
    ex = FakeExchange(
        [[Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)]],
    )
    await repo.reconcile_positions(ex, confirm_delay_seconds=0)
    assert ex.read_count == 1


# ----------------------------------------------------------------------
# 7. dry_run: a pause stops writes, not just orders (review item 5)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_closes_nothing(repo):
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex = FakeExchange([[], []], fills=[_sell_fill()])
    seen: list[tuple] = []

    async def cb(name, pnl):
        seen.append((name, pnl))

    result = await repo.reconcile_positions(
        ex, dry_run=True, on_strategy_close=cb, confirm_delay_seconds=0,
    )

    assert result.actions == []
    assert len(result.failures) == 1
    assert result.failures[0].startswith("PAUSED — not closed")
    assert (await _rows(repo, models.PositionRecord))[0].is_open is True
    assert await _rows(repo, models.Trade) == []
    assert seen == []


@pytest.mark.asyncio
async def test_dry_run_places_no_orders_even_if_asked(repo):
    """`dry_run` overrides `close_exchange_orphans` — a caller cannot
    half-pause the bot by mistake."""
    ex = FakeExchange(
        [[Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)]],
        mid=2100.0,
    )

    result = await repo.reconcile_positions(
        ex, dry_run=True, close_exchange_orphans=True, confirm_delay_seconds=0,
    )

    assert ex.orders == []
    assert result.actions == []


@pytest.mark.asyncio
async def test_dry_run_still_reports_a_clean_book_as_clean(repo):
    await _open_row(repo, side="long", size=1.0)
    ex = FakeExchange(
        [[Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)]],
    )
    result = await repo.reconcile_positions(
        ex, dry_run=True, confirm_delay_seconds=0,
    )
    assert result.summary() == "clean"


# ----------------------------------------------------------------------
# 8. Pass 2 bookkeeping (review items 6 and 7)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbookkept_exchange_orphan_close_is_reported(repo, monkeypatch):
    """The close happened, so it stays an action — but an unrecorded
    fill is the exact divergence this work exists to stop, so it is a
    failure too, with the order id to reconcile by hand."""
    await _live_db(repo)
    ex = FakeExchange(
        [[Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)]],
        mid=2100.0,
    )

    async def boom(**_kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setattr(repo, "record_trade", boom)
    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, strategy_symbols=UNIVERSE,
    )

    assert len(result.actions) == 1
    assert len(result.failures) == 1
    assert "UNBOOKKEPT" in result.failures[0]
    assert "order-1" in result.failures[0]


@pytest.mark.asyncio
async def test_pass_two_sees_a_symbol_freed_by_a_wrong_side_close(repo):
    """After pass 1 wrong-side-closes the only BTC row, the exchange's
    BTC position is untracked and must be closed NOW — not one cycle
    later, after a strategy OPEN has already netted against it."""
    await _live_db(repo)
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    ex_short = Position(symbol="BTC", side="short", size=1.0, entry_price=108.0)
    ex = FakeExchange([[ex_short], [ex_short]], fills=[_sell_fill()], mid=108.0)

    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, strategy_symbols=UNIVERSE,
    )

    assert any("wrong-side" in a for a in result.actions)
    assert any("exchange-orphan" in a for a in result.actions)
    assert ex.orders == [("BTC", "buy", 1.0, OrderType.MARKET)]


@pytest.mark.asyncio
async def test_truncated_fill_page_is_logged(repo, caplog):
    """HL caps a fill page at 2000; a silently truncated window would
    price closes at the mid with no explanation."""
    await _open_row(repo, side="long", size=1.0, entry=100.0)
    fills = [
        _sell_fill(oid=i, coin="XRP", time=FILL_MS + i) for i in range(2000)
    ]
    ex = FakeExchange([[], []], fills=fills, mid=105.0)

    with caplog.at_level("WARNING", logger="hypertrade.db.repo"):
        await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert any("may be truncated" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_clean_pass_reports_the_counts(repo, caplog):
    """An INFO line on every pass, so the log proves reconcile ran."""
    await _open_row(repo, symbol="BTC", side="long", size=1.0)
    ex = FakeExchange(
        [[Position(symbol="BTC", side="long", size=1.0, entry_price=100.0)]],
    )

    with caplog.at_level("INFO", logger="hypertrade.db.repo"):
        result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)

    assert result.summary() == "clean"
    assert result.db_open == 1
    assert result.exchange_open == 1
    assert any("Reconcile: clean" in r.message for r in caplog.records)
