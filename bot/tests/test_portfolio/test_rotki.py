"""Rotki provider parsing tests.

The login + cookie-jar flow is harder to unit-test cleanly without a
real Rotki backend, so the focus here is on `_parse_balances` (pure
function) and the empty-snapshot fallback. Live integration is verified
manually after the user has Rotki running.
"""

from __future__ import annotations

import pytest

from hypertrade.portfolio.providers.rotki import (
    _parse_balances,
    _empty_snapshot,
)


def test_parse_balances_documented_shape():
    data = {
        "result": {
            "assets": {
                "ETH": {"amount": "44.5", "usd_value": "193456.78"},
                "BTC": {"amount": "0.5",  "usd_value": "55000.00"},
                "SOL": {"amount": "100",  "usd_value": "20000.00"},
            },
            "liabilities": {},
            "location": {},
        }
    }
    snap = _parse_balances(data)
    # Sorted by USD value desc
    assert [c.symbol for c in snap.coins] == ["ETH", "BTC", "SOL"]
    assert abs(snap.total_value_usd - (193456.78 + 55000 + 20000)) < 1e-3
    eth = snap.coins[0]
    assert eth.count == 44.5
    # Price is value / count
    assert abs(eth.price_usd - (193456.78 / 44.5)) < 1e-3


def test_parse_balances_skips_zero_holdings():
    data = {
        "result": {
            "assets": {
                "ETH":  {"amount": "1.0", "usd_value": "4000.0"},
                "DEAD": {"amount": "0",   "usd_value": "0"},
            }
        }
    }
    snap = _parse_balances(data)
    assert [c.symbol for c in snap.coins] == ["ETH"]


def test_parse_balances_handles_missing_result():
    snap = _parse_balances({})
    assert snap.coins == []
    assert snap.total_value_usd == 0.0


def test_parse_balances_handles_garbage_entries():
    data = {
        "result": {
            "assets": {
                "ETH": {"amount": "1.0", "usd_value": "4000.0"},
                "X":   "not-a-dict",          # parser must skip, not crash
                "Y":   {"amount": "abc"},     # malformed, parser skips
            }
        }
    }
    snap = _parse_balances(data)
    assert [c.symbol for c in snap.coins] == ["ETH"]


def test_empty_snapshot_has_fetched_at():
    snap = _empty_snapshot()
    assert snap.coins == []
    assert snap.fetched_at  # ISO string populated
