"""Unit tests for the fill-matching maths reconcile prices closes with.

Pure functions, no DB — the integration side lives in
`test_reconcile_positions.py`.
"""

from __future__ import annotations

import pytest

from hypertrade.reconcile.fills import (
    realized_pnl,
    select_closing_fills,
    summarize_closing_fills,
)


def _fill(**over):
    f = {
        "coin": "BTC", "side": "A", "sz": "1.0", "px": "100.0",
        "fee": "0.1", "time": 1_000, "oid": 42,
    }
    f.update(over)
    return f


# --- select_closing_fills ---------------------------------------------


def test_long_is_closed_by_a_sell():
    fills = [_fill(side="A"), _fill(side="B")]
    got = select_closing_fills(fills, symbol="BTC", position_side="long")
    assert len(got) == 1
    assert got[0]["side"] == "A"


def test_short_is_closed_by_a_buy():
    fills = [_fill(side="A"), _fill(side="B")]
    got = select_closing_fills(fills, symbol="BTC", position_side="short")
    assert len(got) == 1
    assert got[0]["side"] == "B"


def test_other_coins_are_not_matched():
    fills = [_fill(coin="ETH")]
    assert select_closing_fills(fills, symbol="BTC", position_side="long") == []


def test_since_ms_excludes_older_fills():
    fills = [_fill(time=500), _fill(time=1_500)]
    got = select_closing_fills(
        fills, symbol="BTC", position_side="long", since_ms=1_000,
    )
    assert [f["time"] for f in got] == [1_500]


def test_result_is_oldest_first():
    fills = [_fill(time=3_000), _fill(time=1_000), _fill(time=2_000)]
    got = select_closing_fills(fills, symbol="BTC", position_side="long")
    assert [f["time"] for f in got] == [1_000, 2_000, 3_000]


def test_word_spelled_sides_are_understood():
    """HL sends "A"/"B"; the backfill tool also sees "Buy"/"Sell"."""
    fills = [_fill(side="Sell"), _fill(side="Buy")]
    assert len(select_closing_fills(fills, symbol="BTC", position_side="long")) == 1


# --- summarize_closing_fills ------------------------------------------


def test_single_fill_is_used_verbatim():
    s = summarize_closing_fills([_fill(sz="1.0", px="110.0", fee="0.5")], size=1.0)
    assert s is not None
    assert s.price == pytest.approx(110.0)
    assert s.size == pytest.approx(1.0)
    assert s.fee == pytest.approx(0.5)
    assert s.order_id == "42"


def test_multiple_fills_are_size_weighted():
    fills = [
        _fill(sz="1.0", px="100.0", fee="0.1", time=1),
        _fill(sz="3.0", px="200.0", fee="0.3", time=2),
    ]
    s = summarize_closing_fills(fills, size=4.0)
    assert s is not None
    assert s.price == pytest.approx((100 * 1 + 200 * 3) / 4)
    assert s.fee == pytest.approx(0.4)


def test_overshooting_fill_is_partially_consumed_and_fee_prorated():
    """A netted HL close can be bigger than one strategy's row."""
    s = summarize_closing_fills(
        [_fill(sz="4.0", px="110.0", fee="0.8")], size=1.0,
    )
    assert s is not None
    assert s.size == pytest.approx(1.0)
    assert s.fee == pytest.approx(0.2)  # a quarter of the fill's fee


def test_partial_coverage_still_prices():
    """Better a price from 0.5 of the size than entry_price for all of
    it — the caller records the row's full size at this price."""
    s = summarize_closing_fills([_fill(sz="0.5", px="110.0")], size=1.0)
    assert s is not None
    assert s.size == pytest.approx(0.5)
    assert s.price == pytest.approx(110.0)


def test_no_fills_returns_none():
    assert summarize_closing_fills([], size=1.0) is None


def test_zero_size_returns_none():
    assert summarize_closing_fills([_fill()], size=0.0) is None


def test_unusable_fills_return_none():
    assert summarize_closing_fills([_fill(px="0")], size=1.0) is None
    assert summarize_closing_fills([_fill(sz="0")], size=1.0) is None


def test_bad_numerics_are_skipped_not_fatal():
    fills = [_fill(px="not-a-number", time=1), _fill(px="110.0", time=2)]
    s = summarize_closing_fills(fills, size=1.0)
    assert s is not None
    assert s.price == pytest.approx(110.0)


def test_tid_is_used_when_oid_is_absent():
    f = _fill()
    del f["oid"]
    f["tid"] = 77
    s = summarize_closing_fills([f], size=1.0)
    assert s is not None
    assert s.order_id == "77"


def test_missing_ids_leave_order_id_none():
    f = _fill()
    del f["oid"]
    s = summarize_closing_fills([f], size=1.0)
    assert s is not None
    assert s.order_id is None


# --- realized_pnl -----------------------------------------------------


def test_long_profit():
    assert realized_pnl(
        side="long", entry_price=100, exit_price=110, size=2, fee=1,
    ) == pytest.approx(19.0)


def test_short_profit():
    assert realized_pnl(
        side="short", entry_price=100, exit_price=90, size=2, fee=1,
    ) == pytest.approx(19.0)


def test_short_loss_is_negative():
    """The sign that pnl=0.0 used to hide."""
    assert realized_pnl(
        side="short", entry_price=100, exit_price=110, size=1,
    ) == pytest.approx(-10.0)


def test_fee_always_subtracts():
    flat = realized_pnl(
        side="long", entry_price=100, exit_price=100, size=1, fee=0.5,
    )
    assert flat == pytest.approx(-0.5)
