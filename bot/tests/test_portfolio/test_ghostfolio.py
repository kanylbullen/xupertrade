"""Ghostfolio parser tests.

Live API verification still pending — these test the pure-function
parser against the documented response shape so the parser stays
honest when we wire it up to a real instance.
"""

from __future__ import annotations

from hypertrade.portfolio.providers.ghostfolio import (
    _empty,
    _parse_holdings,
)


SAMPLE = {
    "holdings": [
        {
            "symbol": "ETH",
            "name": "Ethereum",
            "currency": "USD",
            "dataSource": "COINGECKO",
            "assetClass": "CRYPTOCURRENCY",
            "assetSubClass": "CRYPTOCURRENCY",
            "quantity": 44.5,
            "investment": 100000.0,
            "marketPrice": 4343.56,
            "marketValue": 193288.42,
            "valueInBaseCurrency": 193288.42,
            "grossPerformance": 93288.42,
            "grossPerformancePercent": 0.93,
            "netPerformance": 92500.0,
        },
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "currency": "USD",
            "dataSource": "YAHOO",
            "assetClass": "EQUITY",
            "assetSubClass": "STOCK",
            "quantity": 10.0,
            "investment": 1500.0,
            "marketPrice": 220.0,
            "marketValue": 2200.0,
            "valueInBaseCurrency": 2200.0,
            "grossPerformance": 700.0,
            "netPerformance": 700.0,
        },
    ]
}


def test_parses_documented_shape_and_sorts_by_value():
    snap = _parse_holdings(SAMPLE)
    # Sorted by value desc
    assert [c.symbol for c in snap.coins] == ["ETH", "AAPL"]
    eth = snap.coins[0]
    assert eth.count == 44.5
    assert abs(eth.value_usd - 193288.42) < 1e-3
    assert eth.pnl_all_time_usd == 92500.0  # netPerformance preferred
    aapl = snap.coins[1]
    assert aapl.symbol == "AAPL"  # stocks render the same way crypto does


def test_falls_back_from_value_in_base_currency_to_market_value():
    data = {
        "holdings": [{
            "symbol": "BTC",
            "name": "Bitcoin",
            "quantity": 0.5,
            "marketPrice": 100_000.0,
            "marketValue": 50_000.0,
            # valueInBaseCurrency missing
        }]
    }
    snap = _parse_holdings(data)
    assert snap.coins[0].value_usd == 50_000.0


def test_synthesizes_market_price_when_missing():
    """ghostfolio sometimes omits marketPrice for illiquid assets but
    ships value + quantity. We compute price = value / quantity."""
    data = {
        "holdings": [{
            "symbol": "OBSCURE",
            "quantity": 200.0,
            "valueInBaseCurrency": 1000.0,
            # no marketPrice
        }]
    }
    snap = _parse_holdings(data)
    assert snap.coins[0].price_usd == 5.0


def test_skips_zero_quantity_and_garbage():
    data = {
        "holdings": [
            {"symbol": "ETH", "quantity": 1.0, "valueInBaseCurrency": 4000.0},
            {"symbol": "DEAD", "quantity": 0, "valueInBaseCurrency": 0},
            "not-a-dict",
            {"quantity": 1.0},  # no symbol → skip
        ]
    }
    snap = _parse_holdings(data)
    assert [c.symbol for c in snap.coins] == ["ETH"]


def test_empty_for_missing_holdings_key():
    snap = _parse_holdings({"unrelated": "blob"})
    assert snap.coins == []
    assert snap.total_value_usd == 0.0


def test_empty_helper_carries_error_flag():
    snap = _empty(error="HTTP 401")
    assert snap.ok is False
    assert snap.error == "HTTP 401"

    snap_ok = _empty()
    assert snap_ok.ok is True
    assert snap_ok.error == ""
