"""`GET /api/version` and the build-info file behind it (NU-4).

The image bakes `build-info.json`; the endpoint must report it without
the API key, and must degrade to "unknown" rather than fail or echo
arbitrary text when the file is missing or malformed.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hypertrade import api as api_module
from hypertrade import version as version_module
from hypertrade.version import UNKNOWN_SHA, load_build_info

SHA = "0123456789abcdef0123456789abcdef01234567"
BUILT_AT = "2026-09-24T12:00:00Z"


def _write(tmp_path, payload) -> object:
    path = tmp_path / "build-info.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def test_reads_sha_and_build_time(tmp_path):
    path = _write(tmp_path, {"sha": SHA, "built_at": BUILT_AT})
    assert load_build_info(path) == {"sha": SHA, "built_at": BUILT_AT}


def test_short_and_uppercase_sha_accepted_and_normalised(tmp_path):
    path = _write(tmp_path, {"sha": "ABCDEF1", "built_at": BUILT_AT})
    assert load_build_info(path)["sha"] == "abcdef1"


def test_missing_file_is_unknown_not_an_error(tmp_path):
    assert load_build_info(tmp_path / "nope.json") == {
        "sha": UNKNOWN_SHA, "built_at": None,
    }


@pytest.mark.parametrize("payload", [
    "not json",
    '{"sha": "0123abc"',          # a quote in GIT_SHA breaks the printf'd JSON
    json.dumps(["a", "list"]),
    json.dumps({"sha": "unknown", "built_at": BUILT_AT}),   # compose default
    json.dumps({"sha": "", "built_at": ""}),
    json.dumps({"sha": "<script>alert(1)</script>", "built_at": "yesterday"}),
    json.dumps({"sha": 123, "built_at": 456}),
])
def test_malformed_values_report_unknown(tmp_path, payload):
    info = load_build_info(_write(tmp_path, payload))
    assert info["sha"] == UNKNOWN_SHA
    assert info["built_at"] in (None, BUILT_AT)


async def _get(app, path, headers=None):
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async with client.get(path, headers=headers or {}) as resp:
            return resp.status, await resp.json()
    finally:
        await client.close()


async def test_endpoint_is_public_even_with_an_api_key(tmp_path, monkeypatch):
    """Every other data route 401s without X-Api-Key once a key is set;
    this one must not — checking a deploy should not need the key."""
    monkeypatch.setattr(
        version_module, "BUILD_INFO_PATH",
        _write(tmp_path, {"sha": SHA, "built_at": BUILT_AT}),
    )
    monkeypatch.setattr(api_module.settings, "api_key", "s3cret-test-key")
    app = api_module.create_app()

    assert await _get(app, "/api/version") == (
        200, {"sha": SHA, "built_at": BUILT_AT},
    )
    # Sanity: the key really is enforced elsewhere in the same app.
    status, _ = await _get(app, "/api/positions")
    assert status == 401


async def test_endpoint_without_build_info(tmp_path, monkeypatch):
    monkeypatch.setattr(version_module, "BUILD_INFO_PATH", tmp_path / "absent.json")
    app = api_module.create_app()
    assert await _get(app, "/api/version") == (
        200, {"sha": UNKNOWN_SHA, "built_at": None},
    )
