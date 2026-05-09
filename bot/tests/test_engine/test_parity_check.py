"""Tests for the post-trade DB↔exchange parity check.

Catches the partial-fill drift class that produced the 1.1 SOL excess
in the 2026-05-09 incident: HL filled only part of a close order, bot
recorded the partial as full, exchange position remained, DB thought
position was closed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.engine.runner import EngineRunner
from hypertrade.events.types import ErrorOccurred


def _pos(symbol: str, side: str, size: float) -> MagicMock:
    p = MagicMock()
    p.symbol = symbol
    p.side = side
    p.size = size
    return p


_DEFAULT_SZ_DECIMALS = {"BTC": 5, "ETH": 4, "SOL": 2}


def _runner_with(
    db_positions: list,
    exchange_positions: list,
    sz_decimals: dict | None = None,
) -> tuple:
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=db_positions)
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(return_value=exchange_positions)
    # Per-coin szDecimals drives the dynamic parity tolerance (audit M4).
    sz = sz_decimals if sz_decimals is not None else _DEFAULT_SZ_DECIMALS
    exchange.get_size_precision = lambda s, sz=sz: sz.get(s, 4)
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=event_bus, control=MagicMock(),
    )
    return runner, event_bus


@pytest.mark.asyncio
async def test_no_alert_when_db_matches_exchange():
    """DB has BTC long 0.01, exchange has BTC long 0.01 — no alert,
    returns True so the trade flow proceeds."""
    runner, bus = _runner_with(
        db_positions=[_pos("BTC", "long", 0.01)],
        exchange_positions=[_pos("BTC", "long", 0.01)],
    )
    ok = await runner._check_parity_after_trade("BTC")
    assert ok is True
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_no_alert_within_szdecimals_tolerance():
    """HL rounds sizes to per-coin szDecimals. Tiny diffs are normal —
    SOL has szDecimals=2, so a 0.01 diff is within the tolerance."""
    runner, bus = _runner_with(
        db_positions=[_pos("SOL", "long", 2.13)],
        exchange_positions=[_pos("SOL", "long", 2.14)],
    )
    ok = await runner._check_parity_after_trade("SOL")
    assert ok is True
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_alerts_on_partial_fill_drift_sol():
    """Reproduces 2026-05-09: DB says 2.13 SOL, exchange has 3.24 SOL
    (1.11 excess from un-booked partial fills). 1.11 > 0.05 tolerance
    → alert + return False so the caller can refuse to flip-then-open."""
    runner, bus = _runner_with(
        db_positions=[_pos("SOL", "long", 2.13)],
        exchange_positions=[_pos("SOL", "long", 3.24)],
    )
    ok = await runner._check_parity_after_trade("SOL")
    assert ok is False, "mismatch must return False so flip-detect aborts"
    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert isinstance(event, ErrorOccurred)
    assert event.strategy == "parity/SOL"
    assert "PARITY MISMATCH" in event.message
    assert "2.13" in event.message and "3.24" in event.message


@pytest.mark.asyncio
async def test_alerts_when_exchange_has_position_db_does_not():
    """Exchange-side orphan: 0.5 BTC on exchange, 0 in DB. Real bug —
    something opened a position the bot doesn't track. Alert."""
    runner, bus = _runner_with(
        db_positions=[],
        exchange_positions=[_pos("BTC", "long", 0.5)],
    )
    await runner._check_parity_after_trade("BTC")
    bus.publish.assert_called_once()
    event = bus.publish.call_args[0][0]
    assert "PARITY MISMATCH on BTC" in event.message


@pytest.mark.asyncio
async def test_alerts_when_db_has_position_exchange_does_not():
    """DB-side orphan: bot thinks it has an ETH short, exchange shows
    nothing. Likely close happened externally or fill failed silently."""
    runner, bus = _runner_with(
        db_positions=[_pos("ETH", "short", 0.5)],
        exchange_positions=[],
    )
    await runner._check_parity_after_trade("ETH")
    bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_long_short_netting_summed_correctly():
    """Two strategies on the same coin, opposing sides: long 1.0 + short 0.4
    = net 0.6 long. Exchange shows 0.6 long → no mismatch."""
    runner, bus = _runner_with(
        db_positions=[
            _pos("SOL", "long", 1.0),
            _pos("SOL", "short", 0.4),
        ],
        exchange_positions=[_pos("SOL", "long", 0.6)],
    )
    await runner._check_parity_after_trade("SOL")
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_ignores_other_symbols():
    """Mismatch on ETH should NOT alert when we just traded BTC.
    Parity check is per-symbol after a trade — we only verify the coin
    that just changed."""
    runner, bus = _runner_with(
        db_positions=[
            _pos("BTC", "long", 0.01),
            _pos("ETH", "long", 0.5),
        ],
        exchange_positions=[
            _pos("BTC", "long", 0.01),
            _pos("ETH", "long", 999.0),  # huge mismatch but not the symbol
        ],
    )
    await runner._check_parity_after_trade("BTC")
    bus.publish.assert_not_called()


# --- M4: per-coin tolerance derived from szDecimals -----------------------

@pytest.mark.asyncio
async def test_eth_uses_tighter_tolerance_than_sol():
    """Audit M4 regression: pre-fix, every non-BTC coin shared the SOL
    tolerance (5e-2). ETH szDecimals=4 → tolerance should be 1e-3, so
    a 0.04 ETH drift MUST alert (it would have been silently absorbed
    pre-fix since 0.04 < 5e-2)."""
    runner, bus = _runner_with(
        db_positions=[_pos("ETH", "long", 0.50)],
        exchange_positions=[_pos("ETH", "long", 0.54)],
    )
    ok = await runner._check_parity_after_trade("ETH")
    assert ok is False, "ETH 0.04 drift must trip the alert post-M4"
    bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_eth_within_tightened_tolerance_no_alert():
    """ETH 0.0005 diff is still under the 1e-3 tolerance — no alert."""
    runner, bus = _runner_with(
        db_positions=[_pos("ETH", "long", 0.50)],
        exchange_positions=[_pos("ETH", "long", 0.5005)],
    )
    ok = await runner._check_parity_after_trade("ETH")
    assert ok is True
    bus.publish.assert_not_called()


@pytest.mark.asyncio
async def test_btc_tolerance_is_tightest():
    """BTC szDecimals=5 → tolerance 1e-4. 0.0005 diff alerts."""
    runner, bus = _runner_with(
        db_positions=[_pos("BTC", "long", 0.01)],
        exchange_positions=[_pos("BTC", "long", 0.0105)],
    )
    ok = await runner._check_parity_after_trade("BTC")
    assert ok is False
    bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_default_precision_for_unknown_symbol():
    """Coins not cataloged by the exchange's szDecimals map fall back
    to default 4dp (per Exchange base) → tolerance 1e-3. A 0.05 DOGE
    drift trips the alert."""
    runner, bus = _runner_with(
        db_positions=[_pos("DOGE", "long", 100.0)],
        exchange_positions=[_pos("DOGE", "long", 100.05)],
    )
    ok = await runner._check_parity_after_trade("DOGE")
    assert ok is False
    bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_handles_exchange_failure_without_raising():
    """If exchange.get_positions() fails, parity check logs and exits —
    must not propagate to break the trade flow."""
    repo = MagicMock()
    repo.get_open_positions = AsyncMock(return_value=[])
    exchange = MagicMock()
    exchange.get_positions = AsyncMock(side_effect=RuntimeError("HL down"))
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()
    runner = EngineRunner(
        exchange=exchange, strategies=[], repo=repo,
        event_bus=event_bus, control=MagicMock(),
    )
    # Must not raise
    await runner._check_parity_after_trade("BTC")
    bus_publish = event_bus.publish
    bus_publish.assert_not_called()
