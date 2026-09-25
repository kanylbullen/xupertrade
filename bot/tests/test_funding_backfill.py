"""`python -m hypertrade.reports.funding_backfill` (roadmap NU-6.1).

Dry run by default: it must read and report without writing. --apply
writes what is missing, and a rerun writes nothing twice.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from hypertrade.config import settings
from hypertrade.db import models
from hypertrade.db.repo import Repository
from hypertrade.engine import funding
from hypertrade.reports import funding_backfill

TENANT = "abc12345-aaaa-bbbb-cccc-111122223333"
SINCE = datetime(2026, 4, 28, tzinfo=timezone.utc)
SINCE_MS = funding.to_ms(SINCE)
HOUR_MS = 3_600_000


def record(ms: int, usdc: float):
    return {"time": ms, "hash": "0x" + "0" * 64,
            "delta": {"type": "funding", "coin": "BTC", "usdc": str(usdc), "szi": "0.01"}}


@pytest.fixture
async def repo():
    r = Repository("sqlite+aiosqlite:///:memory:", tenant_id=TENANT, mode="testnet")
    await r.init_db()
    yield r
    await r._engine.dispose()


def fake_hl():
    calls = []

    async def fetch(start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return [record(SINCE_MS, -1.5), record(SINCE_MS + HOUR_MS, 0.25)]

    return fetch, calls


async def count(repo) -> int:
    async with repo._session_factory() as session:
        return await session.scalar(select(func.count(models.FundingPayment.id)))


async def test_dry_run_is_the_default_and_writes_nothing(repo, capsys):
    fetch, calls = fake_hl()

    code = await funding_backfill.run(
        ["--since", "2026-04-28", "--mode", "testnet", "--page-pause", "0"],
        fetch=fetch, repo=repo,
    )

    assert code == 0
    assert calls == [(SINCE_MS, None)]
    assert await count(repo) == 0
    out = capsys.readouterr().out
    assert "missing (not written: dry run): 2" in out
    assert "total -1.2500 USDC" in out


async def test_apply_writes_the_missing_events_once(repo, capsys):
    fetch, _ = fake_hl()
    argv = ["--since", "2026-04-28", "--mode", "testnet", "--apply", "--page-pause", "0"]

    assert await funding_backfill.run(argv, fetch=fetch, repo=repo) == 0
    assert await count(repo) == 2
    assert "inserted: 2" in capsys.readouterr().out

    assert await funding_backfill.run(argv, fetch=fetch, repo=repo) == 0
    assert await count(repo) == 2
    out = capsys.readouterr().out
    assert "inserted: 0" in out and "already stored   2" in out


async def test_until_is_exclusive(repo):
    fetch, calls = fake_hl()
    await funding_backfill.run(
        ["--since", "2026-04-28", "--until", "2026-05-01", "--mode", "testnet",
         "--page-pause", "0"],
        fetch=fetch, repo=repo,
    )
    until_ms = funding.to_ms(datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert calls == [(SINCE_MS, until_ms - 1)]


def test_the_environments_account_is_not_used_for_another_mode(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    monkeypatch.setattr(settings, "hyperliquid_account_address", "0xtestnetaccount")
    args = funding_backfill.parse_args(["--since", "2026-04-28", "--mode", "mainnet"])
    with pytest.raises(SystemExit, match="not the mainnet one"):
        funding_backfill.resolve_address(args)

    args = funding_backfill.parse_args(["--since", "2026-04-28", "--mode", "testnet"])
    assert funding_backfill.resolve_address(args) == "0xtestnetaccount"


async def test_no_tenant_refuses_before_reading_anything(monkeypatch, capsys):
    monkeypatch.setattr(settings, "tenant_id", None)
    fetch, calls = fake_hl()
    code = await funding_backfill.run(
        ["--since", "2026-04-28", "--mode", "testnet"], fetch=fetch,
    )
    assert code == 2 and calls == []
    assert "no tenant" in capsys.readouterr().err
