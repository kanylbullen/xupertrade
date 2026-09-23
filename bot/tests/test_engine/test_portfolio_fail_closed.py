"""Redis-backed risk guards fail CLOSED (`analysis-2026-09-15.md` § 4).

- Kill switch: a flag that cannot be read used to fall back to the env
  default (normally False), so a Redis blip switched off a kill switch
  the operator had turned on. It now counts as ACTIVE for opens; closes
  are never blocked. The WARNING is logged once per outage.
- Daily PnL: a failed `set_daily_pnl` was log-only, so a Redis blip plus
  a restart reset the MAX_DAILY_LOSS_USD counter. It now publishes one
  ErrorOccurred per outage and is retried until a write lands; and PnL
  recorded while the day's total could not be LOADED is added to it
  rather than overwriting it.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from hypertrade.config import settings
from hypertrade.engine.portfolio import PortfolioManager
from hypertrade.events.types import ErrorOccurred


def _control(*, kill=None, kill_raises=False, stored=0.0):
    control = MagicMock()
    control.get_daily_pnl = AsyncMock(return_value=stored)
    control.set_daily_pnl = AsyncMock()
    if kill_raises:
        control.is_kill_switch_active = AsyncMock(side_effect=ConnectionError("redis down"))
    else:
        control.is_kill_switch_active = AsyncMock(return_value=kill)
    return control


def _bus():
    bus = MagicMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", False)
    monkeypatch.setattr(settings, "max_daily_loss_usd", 100)


# ----------------------------------------------------------------------
# Kill switch
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreadable_kill_switch_blocks_opens_even_when_env_is_off():
    pm = PortfolioManager(exchange=MagicMock(), control=_control(kill_raises=True))
    assert await pm.check_risk_limits(is_open=True) is False


@pytest.mark.asyncio
async def test_unreadable_kill_switch_never_blocks_closes():
    pm = PortfolioManager(exchange=MagicMock(), control=_control(kill_raises=True))
    assert await pm.check_risk_limits(is_open=False) is True


@pytest.mark.asyncio
async def test_unreadable_kill_switch_warns_once_per_outage(caplog):
    control = _control(kill_raises=True)
    pm = PortfolioManager(exchange=MagicMock(), control=control)

    with caplog.at_level(logging.WARNING, logger="hypertrade.engine.portfolio"):
        for _ in range(5):
            await pm.check_risk_limits(is_open=True)
        unreadable = [r for r in caplog.records if "unreadable" in r.message]
        assert len(unreadable) == 1

        # Redis back → opens follow the flag again, and the next outage
        # warns again.
        control.is_kill_switch_active = AsyncMock(return_value=None)
        assert await pm.check_risk_limits(is_open=True) is True
        control.is_kill_switch_active = AsyncMock(side_effect=ConnectionError())
        await pm.check_risk_limits(is_open=True)
        unreadable = [r for r in caplog.records if "unreadable" in r.message]
        assert len(unreadable) == 2


@pytest.mark.asyncio
async def test_readable_unset_flag_still_follows_env(monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    pm = PortfolioManager(exchange=MagicMock(), control=_control(kill=None))
    assert await pm.check_risk_limits(is_open=True) is False


# ----------------------------------------------------------------------
# Daily PnL persistence
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_failure_publishes_once_and_retries():
    control = _control()
    control.set_daily_pnl = AsyncMock(side_effect=ConnectionError("redis down"))
    bus = _bus()
    pm = PortfolioManager(exchange=MagicMock(), control=control, event_bus=bus)

    await pm.record_pnl(-40.0)
    await pm.record_pnl(-30.0)

    events = [c.args[0] for c in bus.publish.await_args_list]
    assert [e.strategy for e in events] == ["daily-pnl"]
    assert "restart" in events[0].message
    assert control.set_daily_pnl.await_count == 2, "each record retries"
    assert pm._persist_pending is True

    # Redis back: the next risk check writes the full running total.
    control.set_daily_pnl = AsyncMock()
    await pm.check_risk_limits(is_open=False)
    control.set_daily_pnl.assert_awaited_once()
    assert control.set_daily_pnl.await_args.args[1] == pytest.approx(-70.0)
    assert pm._persist_pending is False


@pytest.mark.asyncio
async def test_new_outage_after_recovery_publishes_again():
    control = _control()
    control.set_daily_pnl = AsyncMock(side_effect=ConnectionError())
    bus = _bus()
    pm = PortfolioManager(exchange=MagicMock(), control=control, event_bus=bus)

    await pm.record_pnl(-10.0)
    control.set_daily_pnl = AsyncMock()
    await pm.record_pnl(-10.0)
    control.set_daily_pnl = AsyncMock(side_effect=ConnectionError())
    await pm.record_pnl(-10.0)

    assert bus.publish.await_count == 2


@pytest.mark.asyncio
async def test_persist_alert_retried_until_delivered(caplog):
    """The alert goes over the same Redis whose write failed. Until the
    bus reports delivery, every later failure tries the publish again;
    after that, no more publishes this outage. The ERROR log is once."""
    control = _control()
    control.set_daily_pnl = AsyncMock(side_effect=ConnectionError("redis down"))
    bus = _bus()
    bus.publish = AsyncMock(side_effect=[False, False, True])
    pm = PortfolioManager(exchange=MagicMock(), control=control, event_bus=bus)

    with caplog.at_level(logging.ERROR, logger="hypertrade.engine.portfolio"):
        for _ in range(5):
            await pm.record_pnl(-1.0)

    assert bus.publish.await_count == 3, "two undelivered attempts, then delivered"
    alerts = [r for r in caplog.records if r.message.startswith("Daily PnL")]
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_event_bus_reports_delivery():
    from hypertrade.events.bus import EventBus, NoOpEventBus

    event = ErrorOccurred(strategy="x", message="y")

    unconnected = EventBus(redis_url="redis://unused:6379/0")
    assert await unconnected.publish(event) is False

    failing = EventBus(redis_url="redis://unused:6379/0")
    failing._redis = MagicMock()
    failing._redis.publish = AsyncMock(side_effect=ConnectionError("down"))
    assert await failing.publish(event) is False

    working = EventBus(redis_url="redis://unused:6379/0")
    working._redis = MagicMock()
    working._redis.publish = AsyncMock(return_value=1)
    assert await working.publish(event) is True

    assert await NoOpEventBus().publish(event) is True


@pytest.mark.asyncio
async def test_pnl_recorded_during_failed_load_is_added_not_overwritten():
    """Restart with Redis unreadable: the stored -$80 must not be replaced
    by the -$30 this process saw. Nothing is written before the load
    succeeds; after it, the total is -$110 and the cap blocks opens."""
    control = _control(kill=None)
    control.get_daily_pnl = AsyncMock(side_effect=ConnectionError("redis down"))
    bus = _bus()
    pm = PortfolioManager(exchange=MagicMock(), control=control, event_bus=bus)

    await pm.record_pnl(-30.0)
    control.set_daily_pnl.assert_not_awaited()
    assert [c.args[0].strategy for c in bus.publish.await_args_list] == ["daily-pnl"]

    control.get_daily_pnl = AsyncMock(return_value=-80.0)
    allowed = await pm.check_risk_limits(is_open=True)

    assert pm._daily_pnl == pytest.approx(-110.0)
    control.set_daily_pnl.assert_awaited_once()
    assert control.set_daily_pnl.await_args.args[1] == pytest.approx(-110.0)
    assert allowed is False, "-$110 is past the $100 daily-loss cap"
