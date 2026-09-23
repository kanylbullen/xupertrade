"""Unit tests for the bot API's quiet-heartbeat access logger.

Covers the aiohttp `AccessLogger` subclass (`api._QuietHeartbeatAccessLogger`)
that silences successful `/api/control/heartbeat` and `/health` polls.
Together with demoting the per-strategy per-tick candle line in
engine/runner.py to DEBUG, this was the other dominant source of the
~1,000 log lines/hour per bot found in bot/reports/analysis-2026-09-15.md
§ 1 "Host" — the dashboard watchdog polls both paths every
HEARTBEAT_WATCHDOG_POLL_SECONDS (60s default) per running bot.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web

from hypertrade.api import _QUIET_ACCESS_LOG_PATHS, _QuietHeartbeatAccessLogger


def _logger() -> _QuietHeartbeatAccessLogger:
    return _QuietHeartbeatAccessLogger(logging.getLogger("test.access"), web.AccessLogger.LOG_FORMAT)


def _request(path: str) -> SimpleNamespace:
    return SimpleNamespace(path=path)


def _response(status: int) -> SimpleNamespace:
    return SimpleNamespace(status=status)


def test_quiet_paths_are_exactly_heartbeat_and_health():
    assert _QUIET_ACCESS_LOG_PATHS == frozenset({"/api/control/heartbeat", "/health"})


def test_silences_successful_heartbeat_poll():
    with patch.object(web.AccessLogger, "log") as base_log:
        _logger().log(_request("/api/control/heartbeat"), _response(200), 0.001)
    base_log.assert_not_called()


def test_silences_successful_health_poll():
    with patch.object(web.AccessLogger, "log") as base_log:
        _logger().log(_request("/health"), _response(204), 0.001)
    base_log.assert_not_called()


def test_silences_boundary_of_2xx_range():
    with patch.object(web.AccessLogger, "log") as base_log:
        _logger().log(_request("/health"), _response(299), 0.001)
    base_log.assert_not_called()


def test_still_logs_non_2xx_heartbeat_response():
    """A 401/5xx on these paths DOES indicate a problem — must not be silenced."""
    with patch.object(web.AccessLogger, "log") as base_log:
        _logger().log(_request("/api/control/heartbeat"), _response(401), 0.001)
    base_log.assert_called_once()


def test_still_logs_5xx_health_response():
    with patch.object(web.AccessLogger, "log") as base_log:
        _logger().log(_request("/health"), _response(503), 0.001)
    base_log.assert_called_once()


def test_logs_every_other_route_unconditionally():
    with patch.object(web.AccessLogger, "log") as base_log:
        _logger().log(_request("/api/positions"), _response(200), 0.001)
    base_log.assert_called_once()
