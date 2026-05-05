"""CoinStats client parsing + degradation tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hypertrade.portfolio.coinstats import (
    _parse_holding,
    fetch_portfolio_coins,
)


# Synthetic response based on the documented shape (ETH entry from the docs)
SAMPLE_RESPONSE = {
    "result": [
        {
            "count": 44.4987,
            "coin": {
                "rank": 2,
                "identifier": "ethereum",
                "symbol": "ETH",
                "name": "Ethereum",
                "icon": "https://example/eth.png",
                "isFake": False,
                "isFiat": False,
                "priceChange24h": -5.74,
                "priceChange1h": 0.1,
                "priceChange7d": 1.12,
                "volume": 61315198931.43,
            },
            "price": {"USD": 4343.56, "BTC": 0.039, "ETH": 1},
            "profit": {
                "allTime": {"USD": 83720.75},
                "hour24": {"USD": -11770.04},
                "unrealized": {"USD": 64875.37},
                "realized": {"USD": 18844.78},
            },
            "averageBuy": {"USD": 2800.50},
            "averageSell": {"USD": 3200.00},
            "liquidityScore": 94.4,
            "volatilityScore": 6.8,
            "marketCapScore": 90.1,
            "riskScore": 7.4,
        },
        {
            # Second coin to exercise sorting
            "count": 0.5,
            "coin": {
                "rank": 1,
                "identifier": "bitcoin",
                "symbol": "BTC",
                "name": "Bitcoin",
                "icon": "https://example/btc.png",
                "priceChange24h": 1.2,
            },
            "price": {"USD": 110_000.0},
            "profit": {
                "allTime": {"USD": 12_000.0},
                "hour24": {"USD": 600.0},
            },
        },
    ]
}


def test_parse_holding_extracts_documented_shape():
    h = _parse_holding(SAMPLE_RESPONSE["result"][0])
    assert h is not None
    assert h.symbol == "ETH"
    assert h.identifier == "ethereum"
    assert h.count == 44.4987
    assert h.price_usd == 4343.56
    # value_usd is precomputed
    assert abs(h.value_usd - (44.4987 * 4343.56)) < 1e-6
    assert h.pnl_24h_usd == -11770.04
    assert h.pnl_all_time_usd == 83720.75
    # priceChange24h is "-5.74" meaning -5.74% — we store as decimal -0.0574
    assert h.price_change_24h_pct is not None
    assert abs(h.price_change_24h_pct - (-0.0574)) < 1e-9
    assert h.risk_score == 7.4


def test_parse_holding_handles_minimal_entry():
    """Real CoinStats responses sometimes drop optional blocks; we should
    still get a usable holding rather than crashing."""
    minimal = {
        "count": 1.0,
        "coin": {"identifier": "btc", "symbol": "BTC", "name": "Bitcoin", "icon": ""},
        "price": {"USD": 100_000.0},
    }
    h = _parse_holding(minimal)
    assert h is not None
    assert h.value_usd == 100_000.0
    assert h.pnl_all_time_usd is None  # missing profit block
    assert h.risk_score is None


def test_parse_holding_skips_garbage_entries():
    assert _parse_holding({}) is None
    assert _parse_holding({"coin": "not-a-dict"}) is None
    # No identifier AND no symbol → skip
    assert _parse_holding({"coin": {}}) is None


def _mock_session_with_response(payload: dict, status: int = 200):
    """Build a context-manager-style mock for aiohttp.ClientSession.get."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        status=status,
        json=AsyncMock(return_value=payload),
        text=AsyncMock(return_value=json.dumps(payload)),
    ))
    cm.__aexit__ = AsyncMock(return_value=None)
    sess = MagicMock()
    sess.get = MagicMock(return_value=cm)
    return sess


@pytest.mark.asyncio
async def test_fetch_portfolio_coins_parses_and_sorts_by_value():
    sess = _mock_session_with_response(SAMPLE_RESPONSE)
    snap = await fetch_portfolio_coins(
        api_key="k", share_token="t", session=sess,
    )
    assert snap.coins
    # ETH stake = 44.5 * $4343 ≈ $193k > BTC stake = 0.5 * $110k = $55k
    assert snap.coins[0].symbol == "ETH"
    assert snap.coins[1].symbol == "BTC"
    # Totals derived from the parsed list
    assert snap.total_value_usd > 0
    assert abs(snap.total_pnl_24h_usd - (-11770.04 + 600.0)) < 1e-3


@pytest.mark.asyncio
async def test_fetch_portfolio_coins_degrades_on_http_error():
    sess = _mock_session_with_response({"error": "nope"}, status=403)
    snap = await fetch_portfolio_coins(
        api_key="k", share_token="t", session=sess,
    )
    assert snap.coins == []
    assert snap.total_value_usd == 0.0
    # We still record fetched_at so the dashboard can show the freshness
    assert snap.fetched_at != ""


@pytest.mark.asyncio
async def test_fetch_portfolio_coins_short_circuits_when_misconfigured():
    snap = await fetch_portfolio_coins(api_key="", share_token="")
    assert snap.coins == []
    # No HTTP call should happen with empty creds — covered by no exception.


@pytest.mark.asyncio
async def test_fetch_portfolio_coins_passes_passcode_header_when_set():
    """Spy on the headers passed to session.get to confirm passcode is
    only sent when configured."""
    sess = _mock_session_with_response(SAMPLE_RESPONSE)
    await fetch_portfolio_coins(
        api_key="k", share_token="t", passcode="123456", session=sess,
    )
    call = sess.get.call_args
    assert call.kwargs["headers"]["passcode"] == "123456"

    sess2 = _mock_session_with_response(SAMPLE_RESPONSE)
    await fetch_portfolio_coins(api_key="k", share_token="t", session=sess2)
    assert "passcode" not in sess2.get.call_args.kwargs["headers"]
