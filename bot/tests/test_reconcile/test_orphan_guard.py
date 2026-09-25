"""NU-2 guard 2: reconcile pass 2 holds an exchange orphan instead of
market-closing it unless it is a lone orphan on a coin a strategy in
this bot trades.

A restored (stale) DB, or a human's position on the account, both look
like exchange positions with no DB row. Before the guard, pass 2
market-closed every one of them in the first pass — a restore
liquidated the book. Real `Repository` on SQLite; only the exchange is
faked.
"""

from __future__ import annotations

import pytest

from hypertrade.db.repo import Repository
from hypertrade.exchange.base import Order, OrderStatus, OrderType, Position

ETH = Position(symbol="ETH", side="long", size=2.0, entry_price=2000.0)
SOL = Position(symbol="SOL", side="short", size=10.0, entry_price=150.0)
CLOSEABLE = {"traded_symbols": {"ETH", "SOL"}}


class FakeExchange:
    """`reads` scripts the answers of successive `get_positions()` calls
    (a list of positions or an exception); `positions` answers after."""

    def __init__(self, positions, reads=()):
        self.positions = list(positions)
        self.reads = list(reads)
        self.orders: list[tuple] = []

    async def get_positions(self):
        if self.reads:
            answer = self.reads.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return list(answer)
        return list(self.positions)

    async def fetch_user_fills(self, address=None, since_ms=None):
        return []

    async def get_current_price(self, symbol):
        return 2100.0

    async def place_order(self, symbol, side, size, order_type=OrderType.MARKET, **kw):
        self.orders.append((symbol, side, size))
        return Order(
            id=f"order-{len(self.orders)}", symbol=symbol, side=side,
            size=size, order_type=order_type, filled_price=2100.0,
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


async def _trades(repo):
    return await repo.get_recent_trades(limit=50)


def _held(result, symbol):
    return [f for f in result.failures if f.startswith("HELD") and symbol in f]


async def test_lone_orphan_on_a_traded_coin_is_closed_in_the_same_pass(repo):
    """The one case pass 2 still closes, exactly as before the guard —
    in this pass, before a strategy OPEN can net against it."""
    ex = FakeExchange([ETH])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == [("ETH", "sell", 2.0)]
    assert result.held_orphans == []
    assert len(await _trades(repo)) == 1


async def test_defaults_hold_everything(repo):
    """A caller that passes no guard input orders nothing."""
    ex = FakeExchange([ETH])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0)
    assert ex.orders == []
    assert result.held_orphans == ["ETH"]


async def test_reconcile_hold_places_no_order(repo):
    ex = FakeExchange([ETH])
    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, hold_orphans="reconcile hold is set",
        **CLOSEABLE,
    )
    assert ex.orders == []
    assert _held(result, "ETH")
    assert "reconcile hold is set" in _held(result, "ETH")[0]
    assert result.took_action  # so the runner publishes it
    assert await _trades(repo) == []


async def test_two_orphans_in_one_pass_are_both_held(repo):
    """A restored DB orphans many coins at once; one close per coin is
    the liquidation the guard exists to stop."""
    ex = FakeExchange([ETH, SOL])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == []
    assert sorted(result.held_orphans) == ["ETH", "SOL"]
    assert "2 exchange orphans" in _held(result, "SOL")[0]


async def test_dust_and_tracked_coins_do_not_count_as_orphans(repo):
    """Only real untracked positions count toward 'more than one'."""
    await repo.open_position(
        strategy_name="kalman_breakout", symbol="SOL", side="short",
        size=10.0, entry_price=150.0,
    )
    dust = Position(symbol="DOGE", side="long", size=1e-9, entry_price=0.1)
    ex = FakeExchange([ETH, SOL, dust])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == [("ETH", "sell", 2.0)]
    assert result.held_orphans == []


async def test_orphan_on_a_coin_no_strategy_trades_is_held(repo):
    """A manual position on some other coin is not ours to close."""
    ex = FakeExchange([ETH])
    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, traded_symbols={"BTC"},
    )
    assert ex.orders == []
    assert "no strategy in this bot trades" in _held(result, "ETH")[0]


async def test_paused_pass_holds_nothing_and_orders_nothing(repo):
    ex = FakeExchange([ETH])
    result = await repo.reconcile_positions(
        ex, confirm_delay_seconds=0, dry_run=True, **CLOSEABLE,
    )
    assert ex.orders == []
    assert result.held_orphans == []


async def test_held_orphan_writes_no_row(repo):
    ex = FakeExchange([ETH, SOL])
    await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert await _trades(repo) == []
    assert await repo.get_open_positions() == []


async def test_an_untraded_orphan_does_not_make_a_traded_one_several(repo):
    """A human's position on a coin nobody here trades must not escalate
    a leftover of ours: the ETH is closed, the DOGE held."""
    doge = Position(symbol="DOGE", side="long", size=100.0, entry_price=0.1)
    ex = FakeExchange([doge, ETH])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == [("ETH", "sell", 2.0)]
    assert result.held_orphans == ["DOGE"]


@pytest.mark.parametrize("second", [[ETH, SOL], ConnectionError("502")])
async def test_a_close_needs_a_re_read_with_the_same_orphans(repo, second):
    """One read can miss a position: ETH and SOL on the book, the first
    read shows only ETH. The re-read before the close sees SOL too (or
    fails), so nothing is closed this pass."""
    ex = FakeExchange([ETH, SOL], reads=[[ETH], second])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == []
    assert "re-read did not confirm" in _held(result, "ETH")[0]
    assert result.held_orphans == []  # deferred: a read blip sets no hold

    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == []  # the next pass sees both
    assert sorted(result.held_orphans) == ["ETH", "SOL"]


@pytest.mark.parametrize("now", [
    Position(symbol="ETH", side="long", size=1.0, entry_price=2000.0),
    Position(symbol="ETH", side="short", size=2.0, entry_price=2000.0),
])
async def test_a_close_needs_the_re_read_to_show_the_same_side_and_size(repo, now):
    """The close is sized and sided from the first read. A human who cut
    or reversed the position between the reads defers it — closing the
    old size would over-close or double it — and the next pass closes
    what is there."""
    ex = FakeExchange([now], reads=[[ETH], [now]])
    result = await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    assert ex.orders == []
    assert "re-read did not confirm" in _held(result, "ETH")[0]
    assert result.held_orphans == []

    await repo.reconcile_positions(ex, confirm_delay_seconds=0, **CLOSEABLE)
    close = "buy" if now.side == "short" else "sell"
    assert ex.orders == [("ETH", close, now.size)]
