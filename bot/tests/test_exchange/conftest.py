"""HTTP-level fakes for the HyperLiquid SDK.

`hl_http` swaps `requests.Session.post` — the one call every SDK request
goes through (`hyperliquid/api.py:API.post`) — for a router, so tests can
drive the real `Info` / `Exchange` classes, their JSON handling and their
error mapping, without a network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import patch

import pytest
import requests

from hypertrade.config import settings

META = {"universe": [
    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
    {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
]}

SPOT_META = {
    "universe": [
        {"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True},
    ],
    "tokens": [
        {"name": "USDC", "szDecimals": 8, "weiDecimals": 8, "index": 0,
         "tokenId": "0x0", "isCanonical": True},
        {"name": "PURR", "szDecimals": 0, "weiDecimals": 5, "index": 1,
         "tokenId": "0x1", "isCanonical": True},
    ],
}

EMPTY_ACCOUNT = {
    "assetPositions": [],
    "marginSummary": {"accountValue": "900.0", "totalMarginUsed": "0.0",
                      "totalNtlPos": "0.0", "totalRawUsd": "900.0"},
    "crossMarginSummary": {"accountValue": "900.0", "totalMarginUsed": "0.0",
                           "totalNtlPos": "0.0", "totalRawUsd": "900.0"},
    "withdrawable": "900.0",
}

HTML_PAGE = "<html><body><h1>502 Bad Gateway</h1>cloudflare</body></html>"


def http_response(status: int, body: object) -> requests.Response:
    """A real `requests.Response`; a str body is sent verbatim, anything
    else as JSON."""
    resp = requests.Response()
    resp.status_code = status
    resp.encoding = "utf-8"
    raw = body if isinstance(body, str) else json.dumps(body)
    resp._content = raw.encode()
    return resp


class HLHttp:
    """Routes SDK POSTs by the request's `type` field.

    A route is a list of answers consumed in order (the last one repeats);
    each answer is `(status, body)` or an exception instance to raise.
    Unrouted types fail the test loudly.
    """

    def __init__(self) -> None:
        self.routes: dict[str, list] = {}
        self.calls: list[str] = []

    def route(self, kind: str, *answers) -> None:
        self.routes[kind] = list(answers)

    def count(self, kind: str) -> int:
        return self.calls.count(kind)

    # Installed on the class as a bound method of this object, so it is
    # called without the session: `session.post(url, json=…, timeout=…)`.
    def _post(self, url, json=None, timeout=None, **_kw):
        kind = (json or {}).get("type") or (json or {}).get("action", {}).get("type")
        self.calls.append(kind)
        if kind not in self.routes:
            raise AssertionError(f"unexpected HL POST {url} type={kind!r}")
        answers = self.routes[kind]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return http_response(status, body)


@pytest.fixture
def hl_http():
    fake = HLHttp()
    fake.route("meta", (200, META))
    fake.route("spotMeta", (200, SPOT_META))
    with patch.object(requests.Session, "post", fake._post):
        yield fake


@pytest.fixture
def build_exchange(hl_http) -> Callable:
    """Construct a real `HyperLiquidExchange` against `hl_http`."""
    from hypertrade.exchange import hyperliquid as hl_module

    made = []

    def _build(attempts: int = 3):
        with patch.object(settings, "hl_init_retry_attempts", attempts), \
             patch.object(settings, "hl_init_retry_backoff_seconds", 0.0), \
             patch.object(settings, "hyperliquid_private_key", "0x" + "1" * 64):
            ex = hl_module.HyperLiquidExchange()
        made.append(ex)
        return ex

    yield _build
    for ex in made:
        ex._executor.shutdown(wait=False, cancel_futures=True)
