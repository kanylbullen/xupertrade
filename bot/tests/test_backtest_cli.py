"""The backtest CLI saves each run under a tenant, and says so when it can't.

`python -m hypertrade.backtest` used to write `backtest_runs` rows with no
`tenant_id`. On Postgres that column is NOT NULL (alembic 0011), so every
save raised, the CLI printed "DB save failed" and exited 0; the dashboard's
tenant-scoped /backtests page never saw a CLI run.

These tests drive `main()` end to end against a SQLite file: real
argument parsing, a real registered strategy and the real Repository,
with only the candle fetch stubbed. SQLite differs from production in two
ways that matter here, both covered by the CLI rather than the schema:
`tenant_id` is nullable in the ORM model SQLite is built from (the CLI
refuses to save without a tenant before any INSERT), and SQLite does not
enforce the `tenants` foreign key (on Postgres an unknown `--tenant-id`
fails the INSERT, which ends the run with exit 1 like any save failure).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from hypertrade.backtest import __main__ as cli
from hypertrade.config import settings
from hypertrade.db.models import BacktestRun
from hypertrade.db.repo import Repository

TENANT = "abc12345-aaaa-bbbb-cccc-111122223333"
ENV_TENANT = "def67890-aaaa-bbbb-cccc-444455556666"
STRATEGY = "rsi_momentum"


def _candles(n: int = 120) -> pd.DataFrame:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = 100 + 10 * np.sin(np.arange(n) / 6)
    return pd.DataFrame({
        "timestamp": [base + timedelta(hours=4 * i) for i in range(n)],
        "open": closes,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": 1000.0,
    })


@pytest.fixture
def fetches(monkeypatch):
    """Stub the candle fetch; records each call so a test can assert
    that nothing was fetched when the CLI refused up front."""
    calls: list[str] = []

    async def fake_fetch(symbol, timeframe, days):
        calls.append(symbol)
        return _candles()

    monkeypatch.setattr(cli, "_fetch_long_window", fake_fetch)
    return calls


@pytest.fixture
async def db_url(tmp_path, monkeypatch):
    """A SQLite file with the bot's tables, set as DATABASE_URL.

    A file rather than `:memory:`: the CLI builds its own Repository and
    engine, and every in-memory connection is a separate empty database.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'bt.db'}"
    setup = Repository(url, tenant_id=None)
    await setup.init_db()
    await setup.close()
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "tenant_id", None)
    return url


async def _rows(url: str) -> list[BacktestRun]:
    repo = Repository(url, tenant_id=None)
    try:
        async with repo._session_factory() as session:
            return list((await session.scalars(select(BacktestRun))).all())
    finally:
        await repo.close()


async def test_save_creates_a_row_for_the_given_tenant(db_url, fetches):
    code = await cli.main(["--strategy", STRATEGY, "--tenant-id", TENANT])

    assert code == 0
    rows = await _rows(db_url)
    assert len(rows) == 1
    assert rows[0].tenant_id == uuid.UUID(TENANT)
    assert rows[0].strategy_name == STRATEGY
    assert rows[0].symbol and rows[0].timeframe


async def test_tenant_defaults_to_tenant_id_from_settings(db_url, fetches, monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", ENV_TENANT)

    assert await cli.main(["--strategy", STRATEGY]) == 0

    rows = await _rows(db_url)
    assert [r.tenant_id for r in rows] == [uuid.UUID(ENV_TENANT)]


async def test_flag_wins_over_settings(db_url, fetches, monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", ENV_TENANT)

    assert await cli.main(["--strategy", STRATEGY, "--tenant-id", TENANT]) == 0

    rows = await _rows(db_url)
    assert [r.tenant_id for r in rows] == [uuid.UUID(TENANT)]


async def test_no_tenant_refuses_before_running(db_url, fetches):
    code = await cli.main(["--strategy", STRATEGY])

    assert code == 2
    assert fetches == []
    assert await _rows(db_url) == []


async def test_no_save_needs_no_tenant(db_url, fetches):
    assert await cli.main(["--strategy", STRATEGY, "--no-save"]) == 0
    assert len(fetches) == 1
    assert await _rows(db_url) == []


async def test_failed_save_exits_non_zero(tmp_path, fetches, monkeypatch, capsys):
    # A database with no tables: the INSERT fails the way a down or
    # unmigrated database would.
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    )

    code = await cli.main(["--strategy", STRATEGY, "--tenant-id", TENANT])

    assert code == 1
    assert f"DB save failed for {STRATEGY}" in capsys.readouterr().err


async def test_invalid_tenant_flag_is_a_usage_error(db_url, fetches):
    with pytest.raises(SystemExit) as exc:
        await cli.main(["--strategy", STRATEGY, "--tenant-id", "operator"])
    assert exc.value.code == 2
    assert fetches == []


async def test_invalid_tenant_from_settings_is_a_usage_error(db_url, fetches, monkeypatch):
    monkeypatch.setattr(settings, "tenant_id", "not-a-uuid")

    assert await cli.main(["--strategy", STRATEGY]) == 2
    assert fetches == []


async def test_unknown_strategy_exits_non_zero(db_url, fetches):
    code = await cli.main(["--strategy", "no_such_strategy", "--tenant-id", TENANT])

    assert code == 1
    assert await _rows(db_url) == []


async def test_repository_refuses_a_tenantless_run(db_url):
    repo = Repository(db_url, tenant_id=None)
    try:
        with pytest.raises(ValueError, match="no tenant"):
            await repo.save_backtest_run(
                strategy_name=STRATEGY, symbol="BTC", timeframe="4h",
                leverage=1,
                period_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                period_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
                days=31.0, initial_equity=10_000.0, final_equity=10_000.0,
                total_return_pct=0.0, apr=0.0, sharpe=0.0,
                max_drawdown_pct=0.0, num_trades=0, num_round_trips=0,
                wins=0, losses=0, win_rate=0.0, fees_paid=0.0,
                position_size_usd=1_000.0, fee_rate=0.00045, slippage_bps=5.0,
            )
    finally:
        await repo.close()
    assert await _rows(db_url) == []
