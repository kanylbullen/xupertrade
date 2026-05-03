"""Verify PaperExchange persists state across instances via Redis.

Without persistence, every container restart wipes the in-memory paper
positions, then reconcile_positions() orphan-closes every DB row, then
strategies re-enter on the next signal — producing duplicate entries.
"""

import pytest

from hypertrade.exchange.paper import PaperExchange


@pytest.mark.asyncio
async def test_paper_state_persists_across_instances():
    """A new PaperExchange instance after load_state() should see the
    positions opened on the previous instance, given Redis is reachable."""
    redis_available = True
    ex1 = PaperExchange(initial_balance=10_000)
    try:
        await ex1.load_state()
        if ex1._redis is None:
            pytest.skip("Redis not reachable in test env")
    except Exception:
        pytest.skip("Redis not reachable in test env")

    # Clear any prior state
    await ex1._redis.delete("paper_exchange:state")
    ex1._positions = {}
    ex1._balance = 10_000
    await ex1._persist()

    # Open a position
    ex1.set_price("BTC", 50_000)
    await ex1.place_order("BTC", "buy", 0.1)
    assert "BTC" in ex1._positions

    # New instance — fresh process, fresh in-memory state
    ex2 = PaperExchange(initial_balance=10_000)
    await ex2.load_state()
    assert "BTC" in ex2._positions
    assert ex2._positions["BTC"].size == pytest.approx(0.1)
    assert ex2._positions["BTC"].entry_price == pytest.approx(50_000)

    # Cleanup
    await ex2._redis.delete("paper_exchange:state")
