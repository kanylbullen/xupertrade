"""`python -m hypertrade.reports.funding_backfill` (roadmap NU-6.1).

Dry run by default: it must read and report without writing. --apply
writes what is missing, and a rerun writes nothing twice. A run that
stops early says so and exits non-zero, with what it did.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
DRY_RUN = ["--since", "2026-04-28", "--mode", "testnet", "--page-pause", "0"]
APPLY = [*DRY_RUN, "--apply"]


def record(ms: int, usdc: float):
    return {"time": ms, "hash": "0x" + "0" * 64,
            "delta": {"type": "funding", "coin": "BTC", "usdc": str(usdc), "szi": "0.01"}}


@pytest.fixture
async def repo(monkeypatch):
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    r = Repository("sqlite+aiosqlite:///:memory:", tenant_id=TENANT)
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
    # Held over the first event only, so the report splits it off.
    async with repo._session_factory() as session:
        session.add(models.PositionRecord(
            tenant_id=uuid.UUID(TENANT), strategy_name="held", symbol="BTC",
            side="long", size=0.01, entry_price=100.0, mode="testnet",
            is_paper=False, is_open=False, opened_at=SINCE - timedelta(hours=1),
            closed_at=SINCE + timedelta(minutes=30),
        ))
        await session.commit()
    fetch, calls = fake_hl()

    code = await funding_backfill.run(DRY_RUN, fetch=fetch, repo=repo)

    assert code == 0
    assert calls == [(SINCE_MS, None)]
    assert await count(repo) == 0
    out = capsys.readouterr().out.splitlines()
    assert "  missing (not written: dry run): 2   -1.2500 USDC, 1 with no position" in out
    assert "  events read      2   total -1.2500 USDC" in out
    assert f"    {'BTC':<10} -1.2500" in out
    assert f"    {'held':<24} -1.5000" in out
    assert f"    {'(no position)':<24} +0.2500" in out


async def test_apply_writes_the_missing_events_once(repo, capsys):
    fetch, _ = fake_hl()

    assert await funding_backfill.run(APPLY, fetch=fetch, repo=repo) == 0
    assert await count(repo) == 2
    assert "inserted: 2" in capsys.readouterr().out

    assert await funding_backfill.run(APPLY, fetch=fetch, repo=repo) == 0
    assert await count(repo) == 2
    out = capsys.readouterr().out
    assert "inserted: 0" in out and "already stored   2" in out


async def test_a_failed_read_reports_what_was_written_and_exits_non_zero(repo, capsys):
    """The exchange read is strict for the backfill: after its retries it
    raises, and the run must not look finished."""
    first = [record(SINCE_MS + i * HOUR_MS, -0.01) for i in range(funding.PAGE_CAP)]
    answers = [first, RuntimeError("userFunding: 502 after 3 tries")]

    async def fetch(start_ms, end_ms):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    code = await funding_backfill.run(APPLY, fetch=fetch, repo=repo)

    assert code == 1
    assert await count(repo) == funding.PAGE_CAP  # the first page stays
    out = capsys.readouterr().out
    assert f"inserted: {funding.PAGE_CAP}" in out
    last = SINCE + timedelta(hours=funding.PAGE_CAP - 1)
    assert f"INCOMPLETE: stopped after {last}" in out


async def test_another_modes_environment_refuses_before_reading(repo, monkeypatch, capsys):
    """The environment's account, tenant and database are its own mode's."""
    monkeypatch.setattr(settings, "exchange_mode", "mainnet")
    fetch, calls = fake_hl()

    code = await funding_backfill.run(DRY_RUN, fetch=fetch, repo=repo)

    assert code == 2 and calls == []
    assert "EXCHANGE_MODE=mainnet" in capsys.readouterr().err


async def test_no_tenant_refuses_before_reading_anything(monkeypatch, capsys):
    monkeypatch.setattr(settings, "exchange_mode", "testnet")
    monkeypatch.setattr(settings, "tenant_id", None)
    fetch, calls = fake_hl()
    code = await funding_backfill.run(DRY_RUN, fetch=fetch)
    assert code == 2 and calls == []
    assert "no tenant" in capsys.readouterr().err
