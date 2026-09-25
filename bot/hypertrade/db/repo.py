"""Database repository for trades and positions."""

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hypertrade.config import settings
from hypertrade.db.models import (
    Base,
    BacktestRun,
    EquitySnapshot,
    FundingPayment,
    HodlPurchase,
    ManualOnchainLevel,
    PositionRecord,
    Tenant,
    TenantAuditLog,
    TenantBot,
    TenantSecret,
    TenantTelegramLink,
    Trade,
    UserVaultEntry,
    Vault,
    VaultNavPoint,
    VaultSnapshot,
)
from hypertrade.reconcile.fills import FillLedger, fill_order_ids

logger = logging.getLogger(__name__)

# HyperLiquid caps `userFillsByTime` at 2000 records per response and
# does not page here. Reconcile asks for a window starting at the oldest
# closeable row's `opened_at`, which on a long-held position can be
# months wide — if the account is busy enough to hit the cap, the recent
# closing fill we actually need may be missing and the row prices at the
# mid instead. Cheap to detect, so we say so rather than silently
# degrading.
_HL_FILL_PAGE_CAP = 2000


# Tables that alembic is the sole authority for — `init_db()` skips
# them so a fresh-bot start can't race-create them ahead of `alembic
# upgrade head`. Hit by the multi-tenancy Phase 1 deploy 2026-05-10:
# bot's `Base.metadata.create_all` raced ahead of alembic on the new
# tenant tables and left them with no `tenant_id`-columns on existing
# tables (alembic crashed on "already exists"). Operator must run
# `alembic upgrade head` once when deploying any new MT phase.
_ALEMBIC_OWNED_TABLES = frozenset({
    Tenant.__tablename__,
    TenantBot.__tablename__,
    TenantSecret.__tablename__,
    TenantAuditLog.__tablename__,
    # PR 3a alembic 0012 — create_all() would race-create this
    # without the indexes + defaults the migration sets up.
    TenantTelegramLink.__tablename__,
})


@dataclass
class ReconcileResult:
    """What one reconcile pass did — or why it refused to do anything.

    `actions` used to be a bare `list[str]`, which made "nothing was
    wrong" and "we never got an answer from the exchange" the same empty
    list. The runner logged both as silence. `skipped` is the difference
    (`bot/reports/analysis-2026-09-15.md` § 2).
    """

    actions: list[str] = field(default_factory=list)
    """Closes that actually happened. A rejected order is NOT one."""

    failures: list[str] = field(default_factory=list)
    """Things reconcile wanted to do and could not. Operator-visible."""

    skipped: str | None = None
    """Set when the pass bailed out; the reason, for logs and Telegram."""

    db_open: int = 0
    exchange_open: int = 0

    held_orphans: list[str] = field(default_factory=list)
    """Symbols pass 2 held for a human instead of closing (NU-2)."""

    unresolved_orphans: list[str] = field(default_factory=list)
    """Symbols pass 2 meant to close and did not — a re-read did not
    confirm them, or the close failed. Such a pass does not count (NU-2)."""

    @property
    def took_action(self) -> bool:
        return bool(self.actions or self.failures)

    def summary(self) -> str:
        if self.skipped:
            return f"skipped: {self.skipped}"
        parts = list(self.actions) + list(self.failures)
        return "; ".join(parts) if parts else "clean"


@dataclass
class _OrphanClosePrice:
    """What reconcile decided a vanished position closed at."""

    exit_price: float
    fee: float
    pnl: float
    order_id: str | None
    reason: str
    estimated: bool


def _orphan_hold_reason(
    symbol: str,
    traded_count: int,
    hold_orphans: str | None,
    traded_symbols: set[str] | frozenset[str],
) -> str | None:
    """Why pass 2 must not market-close this exchange orphan, or None
    (NU-2 guard 2). A lone orphan is usually our own leftover; several on
    traded coins, or one on a coin no strategy here trades, look like a
    restored DB or a human's position — closing those is the liquidation
    the guard exists to stop."""
    if hold_orphans:
        return hold_orphans
    if symbol not in traded_symbols:
        return "no strategy in this bot trades this coin"
    if traded_count > 1:
        return f"{traded_count} exchange orphans on traded coins in one pass"
    return None


def _to_epoch_ms(value: datetime | None) -> int | None:
    """Epoch milliseconds for a DB timestamp.

    SQLite hands back naive datetimes even for `DateTime(timezone=True)`
    columns; everything we store is UTC, so assume UTC rather than the
    process-local zone (which on the devserver is UTC and on a laptop is
    not — a silent hour of drift in the fill window).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


class Repository:
    def __init__(
        self,
        database_url: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        url = database_url or settings.database_url
        self._engine = create_async_engine(url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._mode = settings.exchange_mode
        self._is_paper = settings.is_paper
        # Multi-tenancy Phase 3b: when set, every hot-path INSERT this
        # Repository writes carries this tenant_id (Trade, PositionRecord,
        # EquitySnapshot, FundingPayment). When None, falls back to
        # today's tenant-agnostic behavior — operator's pre-cutover
        # 3-mode deploy.
        #
        # NOTE: SELECT/UPDATE/DELETE are NOT yet scoped to tenant_id;
        # queries filter on `mode` only. With multiple tenants in the
        # same DB this could leak/mutate cross-tenant rows. Real
        # isolation lands in Phase 5 via distinct PG roles + RLS —
        # the application-layer approach is too easy to forget on a
        # new query path. Phase 3b is sufficient ONLY when one
        # tenant_id is in play per process (the operator's current
        # deploy + dashboard-spawned single-tenant bots). Multi-tenant
        # beta (Phase 8) requires Phase 5 first.
        #
        # Stored as a uuid.UUID so SQLAlchemy's Uuid column type can
        # serialize cleanly (a bare string trips its `value.hex` path).
        raw_tenant = (
            tenant_id if tenant_id is not None else settings.tenant_id
        )
        self._tenant_id: uuid.UUID | None = (
            uuid.UUID(raw_tenant) if raw_tenant else None
        )

    async def init_db(self) -> None:
        """Create the legacy bot tables (idempotent via SA's checkfirst).

        Multi-tenancy tables (tenants, tenant_bots, tenant_secrets,
        tenant_audit_log) are EXCLUDED — alembic owns them. If alembic
        hasn't been run yet on a fresh deploy, those tables won't
        exist; that's fine because Phase 1-5 of multi-tenancy doesn't
        write to them. Phase 6 cutover backfills + flips constraints
        and assumes alembic is current.
        """
        legacy_tables = [
            t for t in Base.metadata.sorted_tables
            if t.name not in _ALEMBIC_OWNED_TABLES
        ]
        async with self._engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=legacy_tables)
            )
        logger.info(
            "Database tables ready (ensured %d legacy tables exist; %d alembic-owned skipped)",
            len(legacy_tables),
            len(_ALEMBIC_OWNED_TABLES),
        )

    async def record_trade(
        self,
        order_id: str,
        strategy_name: str,
        symbol: str,
        side: str,
        size: float,
        price: float,
        fee: float = 0.0,
        pnl: float | None = None,
        reason: str = "",
    ) -> Trade:
        async with self._session_factory() as session:
            trade = Trade(
                tenant_id=self._tenant_id,
                order_id=order_id,
                strategy_name=strategy_name,
                symbol=symbol,
                side=side,
                size=size,
                price=price,
                fee=fee,
                pnl=pnl,
                reason=reason,
                is_paper=self._is_paper,
                mode=self._mode,
            )
            session.add(trade)
            await session.commit()
            return trade

    async def open_position(
        self,
        strategy_name: str,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        state_json: str | None = None,
    ) -> PositionRecord:
        async with self._session_factory() as session:
            pos = PositionRecord(
                tenant_id=self._tenant_id,
                strategy_name=strategy_name,
                symbol=symbol,
                side=side,
                size=size,
                entry_price=entry_price,
                is_paper=self._is_paper,
                mode=self._mode,
                state_json=state_json,
            )
            session.add(pos)
            await session.commit()
            return pos

    async def record_trade_and_open_position(
        self,
        *,
        order_id: str,
        strategy_name: str,
        symbol: str,
        trade_side: str,         # "buy"/"sell" — exchange side
        position_side: str,      # "long"/"short" — strategy side
        size: float,
        price: float,
        fee: float = 0.0,
        reason: str = "",
        state_json: str | None = None,
    ) -> tuple[Trade, PositionRecord]:
        """Insert Trade + PositionRecord atomically in one transaction.

        Audit M8 (2026-05-09): pre-fix the runner did `record_trade()`
        then `open_position()` in two separate sessions. A SIGTERM /
        crash between the two writes left a Trade row with no matching
        PositionRecord — at next startup the reconcile loop saw the
        exchange position as an orphan and closed it, forcing an
        unintended exit. Atomic commit eliminates that gap entirely.

        OPEN-only — close path uses `record_trade_and_close_position`
        (mirror) since UPDATE-then-INSERT has the same partial-write risk.
        """
        async with self._session_factory() as session:
            async with session.begin():
                trade = Trade(
                    tenant_id=self._tenant_id,
                    order_id=order_id,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    side=trade_side,
                    size=size,
                    price=price,
                    fee=fee,
                    pnl=None,  # opens have no realized pnl
                    reason=reason,
                    is_paper=self._is_paper,
                    mode=self._mode,
                )
                pos = PositionRecord(
                    tenant_id=self._tenant_id,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    side=position_side,
                    size=size,
                    entry_price=price,
                    is_paper=self._is_paper,
                    mode=self._mode,
                    state_json=state_json,
                )
                session.add(trade)
                session.add(pos)
            return trade, pos

    async def record_trade_and_close_position(
        self,
        *,
        order_id: str,
        strategy_name: str,
        symbol: str,
        trade_side: str,
        size: float,
        price: float,
        fee: float = 0.0,
        pnl: float = 0.0,
        reason: str = "",
        remaining: float = 0.0,
    ) -> Trade:
        """Insert Trade + UPDATE matching open PositionRecord atomically.

        `remaining` > 0 is a close that filled short: the row stays open
        with that size, so the strategy's next exit closes the rest.

        Mirror of record_trade_and_open_position for the close path
        (audit M8). Without atomicity, a crash between recording the
        trade and closing the position would leave the position-record
        flagged is_open=true while the trade row says we exited.

        Always returns the Trade row. If no matching open position
        exists, the trade is still recorded (defensive — caller may
        have a reason to log the close-side trade even when the
        position-record is already gone, e.g. earlier reconcile-orphan
        close); only the position UPDATE is skipped in that case.
        """
        async with self._session_factory() as session:
            async with session.begin():
                # Find matching open position
                result = await session.execute(
                    select(PositionRecord).where(
                        PositionRecord.strategy_name == strategy_name,
                        PositionRecord.symbol == symbol,
                        PositionRecord.is_open == True,
                        PositionRecord.mode == self._mode,
                    )
                )
                pos = result.scalar_one_or_none()
                trade = Trade(
                    tenant_id=self._tenant_id,
                    order_id=order_id,
                    strategy_name=strategy_name,
                    symbol=symbol,
                    side=trade_side,
                    size=size,
                    price=price,
                    fee=fee,
                    pnl=pnl,
                    reason=reason,
                    is_paper=self._is_paper,
                    mode=self._mode,
                )
                session.add(trade)
                if pos is not None and remaining > 0:
                    pos.size = remaining
                elif pos is not None:
                    pos.is_open = False
                    pos.exit_price = price
                    pos.pnl = pnl
                    pos.closed_at = datetime.now(timezone.utc)
            return trade

    async def close_position(
        self,
        strategy_name: str,
        symbol: str,
        exit_price: float,
        pnl: float,
    ) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.strategy_name == strategy_name,
                    PositionRecord.symbol == symbol,
                    PositionRecord.mode == self._mode,
                    PositionRecord.is_open == True,
                )
            )
            pos = result.scalar_one_or_none()
            if pos:
                pos.is_open = False
                pos.exit_price = exit_price
                pos.pnl = pnl
                pos.closed_at = datetime.now(timezone.utc)
                await session.commit()

    async def get_open_positions(
        self, strategy_name: str | None = None
    ) -> list[PositionRecord]:
        async with self._session_factory() as session:
            query = select(PositionRecord).where(
                PositionRecord.is_open == True,
                PositionRecord.mode == self._mode,
            )
            if strategy_name:
                query = query.where(PositionRecord.strategy_name == strategy_name)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def snapshot_equity(
        self, total: float, available: float, unrealized_pnl: float
    ) -> None:
        async with self._session_factory() as session:
            snap = EquitySnapshot(
                tenant_id=self._tenant_id,
                total_equity=total,
                available_balance=available,
                unrealized_pnl=unrealized_pnl,
                is_paper=self._is_paper,
                mode=self._mode,
            )
            session.add(snap)
            await session.commit()

    async def update_position_pnl(self, position_id: int, pnl: float) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(PositionRecord.id == position_id)
            )
            pos = result.scalar_one_or_none()
            if pos:
                pos.pnl = pnl
                await session.commit()

    async def get_open_position_any(self, symbol: str) -> PositionRecord | None:
        """The OLDEST open row on `symbol`, any strategy.

        `ORDER BY opened_at, id` makes "which row" deterministic when
        several strategies hold the coin (`allow_multi_coin=True`); a bare
        `LIMIT 1` let Postgres return any of them. Callers that must see
        every holder (the engine's open gates) read all rows instead.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord)
                .where(
                    PositionRecord.symbol == symbol,
                    PositionRecord.mode == self._mode,
                    PositionRecord.is_open == True,
                )
                .order_by(PositionRecord.opened_at, PositionRecord.id)
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def get_open_positions_for_symbol(
        self, symbol: str,
    ) -> list[PositionRecord]:
        """All open position rows for one coin (any strategy).

        When `allow_multi_coin=True` two strategies can hold open rows
        on the same coin. The exchange shows one netted position. Flat-all
        needs to close every DB row, not just one — otherwise the others
        get reconcile-orphan-closed with PnL=0 (audit PR #31 review).
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.symbol == symbol,
                    PositionRecord.mode == self._mode,
                    PositionRecord.is_open == True,
                )
            )
            return list(result.scalars().all())

    async def get_open_position(
        self, strategy_name: str, symbol: str
    ) -> PositionRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.strategy_name == strategy_name,
                    PositionRecord.symbol == symbol,
                    PositionRecord.mode == self._mode,
                    PositionRecord.is_open == True,
                )
            )
            return result.scalar_one_or_none()

    async def get_recent_trades(
        self, limit: int = 50, strategy_name: str | None = None
    ) -> list[Trade]:
        async with self._session_factory() as session:
            query = (
                select(Trade)
                .where(Trade.mode == self._mode)
                .order_by(Trade.timestamp.desc())
                .limit(limit)
            )
            if strategy_name:
                query = query.where(Trade.strategy_name == strategy_name)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def upsert_funding_payment(
        self,
        ts: datetime,
        h: str,
        coin: str,
        usdc: float,
        szi: float | None,
        funding_rate: float | None,
        strategy_name: str | None,
    ) -> bool:
        """Insert a funding payment if not already present (dedup by hash).
        Returns True if inserted, False if already existed."""
        from sqlalchemy.exc import IntegrityError
        async with self._session_factory() as session:
            existing = await session.execute(
                select(FundingPayment.id).where(FundingPayment.hash == h)
            )
            if existing.scalar_one_or_none() is not None:
                return False
            row = FundingPayment(
                tenant_id=self._tenant_id,
                timestamp=ts,
                hash=h,
                coin=coin,
                usdc=usdc,
                szi=szi,
                funding_rate=funding_rate,
                strategy_name=strategy_name,
                is_paper=self._is_paper,
                mode=self._mode,
            )
            session.add(row)
            try:
                await session.commit()
                return True
            except IntegrityError:
                return False

    async def get_funding_since(self, since: datetime) -> list[FundingPayment]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FundingPayment)
                .where(
                    FundingPayment.mode == self._mode,
                    FundingPayment.timestamp >= since,
                )
                .order_by(FundingPayment.timestamp.desc())
            )
            return list(result.scalars().all())

    async def get_latest_funding_timestamp(self) -> datetime | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(FundingPayment.timestamp)
                .where(FundingPayment.mode == self._mode)
                .order_by(FundingPayment.timestamp.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def save_backtest_run(
        self,
        *,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        leverage: int,
        period_start: datetime,
        period_end: datetime,
        days: float,
        initial_equity: float,
        final_equity: float,
        total_return_pct: float,
        apr: float,
        sharpe: float,
        max_drawdown_pct: float,
        num_trades: int,
        num_round_trips: int,
        wins: int,
        losses: int,
        win_rate: float,
        fees_paid: float,
        position_size_usd: float,
        fee_rate: float,
        slippage_bps: float,
    ) -> int:
        """Insert a backtest run summary. Returns the new row's id."""
        async with self._session_factory() as session:
            row = BacktestRun(
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
                leverage=leverage,
                period_start=period_start,
                period_end=period_end,
                days=days,
                initial_equity=initial_equity,
                final_equity=final_equity,
                total_return_pct=total_return_pct,
                apr=apr,
                sharpe=sharpe,
                max_drawdown_pct=max_drawdown_pct,
                num_trades=num_trades,
                num_round_trips=num_round_trips,
                wins=wins,
                losses=losses,
                win_rate=win_rate,
                fees_paid=fees_paid,
                position_size_usd=position_size_usd,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def get_recent_backtest_runs(
        self, limit: int = 50, strategy_name: str | None = None,
    ) -> list[BacktestRun]:
        async with self._session_factory() as session:
            query = (
                select(BacktestRun)
                .order_by(BacktestRun.created_at.desc())
                .limit(limit)
            )
            if strategy_name:
                query = query.where(BacktestRun.strategy_name == strategy_name)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def get_trades_since(self, since: datetime) -> list[Trade]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Trade)
                .where(Trade.mode == self._mode, Trade.timestamp >= since)
                .order_by(Trade.timestamp.desc())
            )
            return list(result.scalars().all())

    async def get_trade_counts_per_strategy(
        self, since: datetime,
    ) -> dict[str, int]:
        """Per-strategy trade count for this mode since `since`.

        Used by the trade-rate anomaly alarm to detect strategies
        spam-trading vs their normal baseline. Returns {} when no
        trades match.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(Trade.strategy_name, func.count(Trade.id))
                .where(Trade.mode == self._mode, Trade.timestamp >= since)
                .group_by(Trade.strategy_name)
            )
            return {name: count for name, count in result.all()}

    async def _open_position_rows(self) -> list[PositionRecord]:
        """Every open DB row for this mode. Read-only snapshot."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.is_open == True,
                    PositionRecord.mode == self._mode,
                )
            )
            return list(result.scalars().all())

    async def _recorded_order_ids(self, order_ids: set[str]) -> set[str]:
        """Which of `order_ids` already have a `trades` row in this mode.

        One query per reconcile pass. Its output is what keeps another
        strategy's already-booked close from being handed to an orphan
        as its own closing fill.
        """
        if not order_ids:
            return set()
        async with self._session_factory() as session:
            result = await session.execute(
                select(Trade.order_id).where(
                    Trade.mode == self._mode,
                    Trade.order_id.in_(list(order_ids)),
                )
            )
            return {oid for oid in result.scalars().all() if oid}

    async def _price_orphan_close(
        self, exchange, row: PositionRecord, ledger,
        mid_cache: dict[str, float], label: str = "orphan close",
    ) -> "_OrphanClosePrice | None":
        """Work out what a row that vanished from the exchange closed at.

        Order of preference, because a guess written into `pnl` is worse
        than no number at all:

        1. The exchange's own closing fill(s) for this coin/side since
           `opened_at`, taken from the pass's `FillLedger` — authoritative
           price AND fee. The ledger spends each fill once and skips
           fills already booked as `trades` rows, so one close can never
           price two rows or double-count a fee.
        2. The current mid price, clearly marked ESTIMATED. Fee is the
           configured taker rate, since the close did cost one.
        3. Nothing. The row stays OPEN and is reported, rather than
           being closed at a fabricated PnL.
        """
        from hypertrade.reconcile.fills import realized_pnl  # noqa: PLC0415

        size = float(row.size)
        entry = float(row.entry_price)
        opened_ms = _to_epoch_ms(row.opened_at)

        summary = ledger.take(
            symbol=row.symbol, position_side=row.side,
            size=size, since_ms=opened_ms,
        )
        if summary is not None:
            return _OrphanClosePrice(
                exit_price=summary.price,
                fee=summary.fee,
                pnl=realized_pnl(
                    side=row.side, entry_price=entry,
                    exit_price=summary.price, size=size, fee=summary.fee,
                ),
                order_id=summary.order_id,
                reason=f"reconcile: {label} (fill)",
                estimated=False,
            )

        mid = mid_cache.get(row.symbol)
        if mid is None:
            try:
                mid = float(await exchange.get_current_price(row.symbol))
            except Exception:
                logger.exception(
                    "Reconcile: mid-price read failed for %s", row.symbol,
                )
                mid = 0.0
            mid_cache[row.symbol] = mid
        if mid > 0:
            fee = mid * size * float(settings.taker_fee_rate)
            return _OrphanClosePrice(
                exit_price=mid,
                fee=fee,
                pnl=realized_pnl(
                    side=row.side, entry_price=entry, exit_price=mid,
                    size=size, fee=fee,
                ),
                order_id=None,
                # ESTIMATED leads the reason so it is the first thing
                # visible in a trades listing: this PnL is booked like a
                # real one (the money did move) but was priced from the
                # mid, not from a fill.
                reason=f"ESTIMATED reconcile: {label} @ mid",
                estimated=True,
            )
        return None

    async def _close_row_priced(
        self, session: AsyncSession, row_id: int,
        priced: "_OrphanClosePrice", now: datetime,
    ) -> PositionRecord | None:
        """Close one open row at `priced` and write its `Trade` row, in
        `session` (the caller commits). None when the row is gone or
        already closed — the normal signal path got there between the
        caller's read and this write — and then nothing is written."""
        db_row = await session.get(PositionRecord, row_id)
        if db_row is None or not db_row.is_open:
            return None
        db_row.is_open = False
        db_row.exit_price = priced.exit_price
        db_row.pnl = priced.pnl
        db_row.closed_at = now
        await session.flush()
        await self._insert_reconcile_trade(
            session,
            preferred_order_id=priced.order_id,
            strategy_name=db_row.strategy_name,
            symbol=db_row.symbol,
            side="sell" if db_row.side == "long" else "buy",
            size=float(db_row.size),
            price=priced.exit_price,
            fee=priced.fee,
            pnl=priced.pnl,
            reason=priced.reason,
            timestamp=now,
        )
        return db_row

    async def close_row_externally(
        self, exchange, row: PositionRecord,
    ) -> "_OrphanClosePrice | None":
        """Book one row the exchange no longer holds as closed outside the
        bot (NU-5a: a strategy exit found it liquidated or closed by hand).

        Priced exactly as reconcile prices an orphan (#167): the
        exchange's own closing fills since the row opened, never one
        already booked as a trade; else the mid, marked ESTIMATED; else
        nothing, and the row stays open. No order is placed. Returns the
        price used, or None when nothing was written.
        """
        ledger = await self._fill_ledger(
            exchange, _to_epoch_ms(row.opened_at), "External close",
        )
        priced = await self._price_orphan_close(
            exchange, row, ledger, {}, label="external close",
        )
        if priced is None:
            return None
        async with self._session_factory() as session:
            closed = await self._close_row_priced(
                session, row.id, priced, datetime.now(timezone.utc),
            )
            if closed is None:
                return None
            await session.commit()
        return priced

    async def _fill_ledger(self, exchange, since_ms, who: str) -> FillLedger:
        """The exchange's fills since `since_ms`, each spendable once and
        none already booked as a trade (#167); empty when the fetch
        fails, so every close prices at the mid."""
        fills: list[dict] = []
        try:
            fills = list(await exchange.fetch_user_fills(since_ms=since_ms) or [])
        except Exception:
            logger.exception(
                "%s: fetch_user_fills failed — falling back to mid price", who,
            )
        if len(fills) >= _HL_FILL_PAGE_CAP:
            logger.warning(
                "%s: fetch_user_fills returned %d records (HL caps a page at "
                "%d) — the window from %s may be truncated and a close older "
                "than the newest %d fills would price at the mid instead",
                who, len(fills), _HL_FILL_PAGE_CAP, since_ms, _HL_FILL_PAGE_CAP,
            )
        ledger = FillLedger(
            fills,
            exclude_order_ids=await self._recorded_order_ids(fill_order_ids(fills)),
        )
        if ledger.excluded_count:
            logger.info(
                "%s: %d of %d fill(s) are already booked as trades rows and "
                "are not available to price an orphan", who,
                ledger.excluded_count,
                ledger.excluded_count + ledger.usable_count,
            )
        return ledger

    @staticmethod
    def _classify_closes(
        db_by_symbol: dict[str, list[PositionRecord]], ex_by_symbol: dict,
    ) -> list[tuple[PositionRecord, str]]:
        """DB rows the exchange does not back. Pure; no writes, no logs."""
        to_close: list[tuple[PositionRecord, str]] = []
        for symbol, db_pos_list in db_by_symbol.items():
            ex_pos = ex_by_symbol.get(symbol)
            if ex_pos is None:
                to_close.extend((p, "orphan") for p in db_pos_list)
                continue
            to_close.extend(
                (p, "wrong-side") for p in db_pos_list if p.side != ex_pos.side
            )
        return to_close

    @staticmethod
    def _log_size_mismatches(
        db_by_symbol: dict[str, list[PositionRecord]], ex_by_symbol: dict,
    ) -> None:
        """Same-side size drift. Logged only — attribution is ambiguous.

        Only flag a diff of more than 0.5% of the position OR more than
        0.1% absolute; smaller diffs are normal exchange-side rounding.
        """
        for symbol, db_pos_list in db_by_symbol.items():
            ex_pos = ex_by_symbol.get(symbol)
            if ex_pos is None:
                continue
            same_side_db_total = sum(
                p.size for p in db_pos_list if p.side == ex_pos.side
            )
            diff = abs(same_side_db_total - ex_pos.size)
            if diff > max(ex_pos.size * 0.005, 1e-4):
                logger.warning(
                    "Reconcile: %s size mismatch — DB total %.6f vs exchange %.6f "
                    "(diff %.6f, strategies: %s)",
                    symbol,
                    same_side_db_total,
                    ex_pos.size,
                    diff,
                    [p.strategy_name for p in db_pos_list if p.side == ex_pos.side],
                )

    async def reconcile_positions(
        self,
        exchange,
        close_exchange_orphans: bool = True,
        on_strategy_close=None,
        confirm_delay_seconds: float = 2.0,
        dry_run: bool = False,
        hold_orphans: str | None = None,
        traded_symbols: set[str] | frozenset[str] = frozenset(),
    ) -> "ReconcileResult":
        """Compare DB open positions vs exchange reality:

        1. DB open + exchange has no position → close DB row (orphan),
           priced from the exchange's closing fill and bookkept with a
           `Trade` row.
        2. DB side ≠ exchange side → same treatment (wrong-side).
        3. Exchange has position + DB has no row → market-close on
           exchange (exchange-side orphan). Untracked positions are
           dangerous: a strategy's next OPEN will net against them,
           producing more DB-vs-exchange divergence. Set
           `close_exchange_orphans=False` to only log them — the runner
           does exactly that while the bot is paused, so a pause
           actually stops orders.
        4. Same-side size mismatch → logged only (ambiguous attribution).

        Case 3 is guarded (NU-2): an orphan is HELD — no order, an entry
        in `failures` and in `held_orphans` — while `hold_orphans` gives
        a reason, when the pass finds more than one orphan, or when no
        strategy in `traded_symbols` trades its coin. The defaults hold
        everything, so a caller has to opt in to a close. A lone orphan
        on a traded coin is still closed in the same pass, before any
        strategy OPEN can net against it.

        Two invariants this function now keeps, both learned the hard way
        (`bot/reports/analysis-2026-09-15.md` § 2 — 31 % of testnet closes
        since May were this bug):

        - **A failed read is not "flat".** If `get_positions()` raises,
          nothing is written and nothing is ordered; the result carries a
          `skipped` marker for the caller to log and publish.
        - **An empty exchange with a non-empty DB gets a second look.**
          A single HL 502 used to flatten the whole book in one pass. We
          now wait `confirm_delay_seconds` and re-read before closing
          anything; a raising re-read skips, and a re-read that finds
          positions means the first answer was a blip.

        `dry_run=True` (the runner passes it while the bot is PAUSED)
        reports every would-be close in `failures` and writes nothing at
        all — no rows closed, no `Trade` rows, no PnL booked, and no
        orders. A pause has to stop writes, not just orders.

        `on_strategy_close(strategy_name, pnl)` is called once per
        DB-side close, AFTER the commit, so a rolled-back close never
        resets a strategy or moves the daily-PnL counter. It may be sync
        or async; an awaitable return value is awaited.
        """
        from hypertrade.exchange.base import (  # noqa: PLC0415
            OrderStatus,
            OrderType,
        )

        result = ReconcileResult()
        if dry_run:
            # Belt and braces: a caller that sets dry_run but forgets
            # close_exchange_orphans still places no orders.
            close_exchange_orphans = False

        # --- 1. Read the exchange. A failure here is NOT "flat". -------
        # ExchangeReadError is the expected type; anything else (a mock,
        # an SDK path we have not seen) gets the same protection.
        try:
            ex_positions = await exchange.get_positions()
        except Exception as e:
            result.skipped = f"exchange read failed: {e}"
            logger.warning(
                "Reconcile: skipped — exchange read raised %s: %s. "
                "No DB change, no orders.", type(e).__name__, e,
            )
            return result

        db_positions = await self._open_position_rows()
        result.db_open = len(db_positions)
        result.exchange_open = len(ex_positions)

        ex_by_symbol = {p.symbol: p for p in ex_positions}
        db_by_symbol: dict[str, list[PositionRecord]] = {}
        for p in db_positions:
            db_by_symbol.setdefault(p.symbol, []).append(p)

        # --- 2. Classify, then confirm before believing it. ------------
        # Any close candidate — an orphan, a wrong-side row, one coin or
        # the whole book — is an expensive claim about a read that can
        # lie. Confirming only the all-empty case still let a partial
        # 502 (ETH answered, BTC missing) close a row.
        to_close = self._classify_closes(db_by_symbol, ex_by_symbol)
        if to_close:
            logger.warning(
                "Reconcile: %d row(s) look closeable against %d exchange "
                "position(s) — re-reading in %.1fs to confirm",
                len(to_close), len(ex_positions), confirm_delay_seconds,
            )
            if confirm_delay_seconds > 0:
                await asyncio.sleep(confirm_delay_seconds)
            try:
                ex_positions = await exchange.get_positions()
            except Exception as e:
                # ExchangeReadError or anything else: without a
                # confirmation we do not act.
                result.skipped = f"confirming exchange read failed: {e}"
                logger.warning(
                    "Reconcile: skipped — confirming read raised %s: %s. "
                    "No DB change, no orders.", type(e).__name__, e,
                )
                return result
            result.exchange_open = len(ex_positions)
            ex_by_symbol = {p.symbol: p for p in ex_positions}
            before = len(to_close)
            to_close = self._classify_closes(db_by_symbol, ex_by_symbol)
            if not to_close:
                logger.warning(
                    "Reconcile: the confirming read cleared all %d candidate "
                    "close(s) — the first read was a blip, not reality",
                    before,
                )
            elif len(to_close) < before:
                logger.warning(
                    "Reconcile: the confirming read cleared %d of %d "
                    "candidate close(s)", before - len(to_close), before,
                )

        self._log_size_mismatches(db_by_symbol, ex_by_symbol)

        # --- 3. Paused means paused: report, write nothing. ------------
        if dry_run and to_close:
            for row, kind in to_close:
                msg = (
                    f"PAUSED — not closed ({kind}): {row.strategy_name} "
                    f"{row.side} {row.size} {row.symbol}"
                )
                result.failures.append(msg)
                logger.warning("Reconcile: %s", msg)
            to_close = []

        # --- 4. Price every close from the exchange before writing. ----
        planned: list[tuple[PositionRecord, str, _OrphanClosePrice]] = []
        if to_close:
            since_ms = min(
                (_to_epoch_ms(p.opened_at) or 0) for p, _ in to_close
            ) or None
            ledger = await self._fill_ledger(exchange, since_ms, "Reconcile")
            mid_cache: dict[str, float] = {}
            for row, kind in to_close:
                priced = await self._price_orphan_close(
                    exchange, row, ledger, mid_cache,
                )
                if priced is None:
                    msg = (
                        f"LEFT OPEN (unpriceable): {row.strategy_name} "
                        f"{row.side} {row.size} {row.symbol} — no closing "
                        f"fill and no mid price; refusing to fabricate a close"
                    )
                    result.failures.append(msg)
                    logger.error("Reconcile: %s", msg)
                    continue
                planned.append((row, kind, priced))

        # --- 5. Apply: close + Trade row in ONE transaction. -----------
        closed: list[tuple[str, float]] = []
        closed_symbols: set[str] = set()
        if planned:
            now = datetime.now(timezone.utc)
            async with self._session_factory() as session:
                for row, kind, priced in planned:
                    db_row = await self._close_row_priced(
                        session, row.id, priced, now,
                    )
                    if db_row is None:
                        continue

                    msg = (
                        f"closed {kind}: {db_row.strategy_name} "
                        f"{db_row.side} {db_row.size} {db_row.symbol} "
                        f"@ {priced.exit_price:.6f} "
                        # "ESTIMATED" in the action text so Telegram shows
                        # it: the PnL is booked like a real one, but the
                        # price came from the mid, not from a fill.
                        f"({'ESTIMATED @ mid' if priced.estimated else 'fill'}), "
                        f"pnl {priced.pnl:+.4f}"
                    )
                    result.actions.append(msg)
                    logger.warning("Reconcile: %s", msg)
                    closed.append((db_row.strategy_name, priced.pnl))
                    closed_symbols.add(db_row.symbol)
                await session.commit()

        # Callbacks only after the commit landed — a rolled-back close
        # must not reset a strategy or move the daily-PnL counter.
        if on_strategy_close:
            for strategy_name, pnl in closed:
                try:
                    maybe = on_strategy_close(strategy_name, pnl)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    logger.exception(
                        "Reconcile: on_strategy_close failed for %s", strategy_name,
                    )

        # --- 6. Pass 2: exchange positions no DB row covers. -----------
        # Outside the session because it places market orders. Threshold:
        # ignore dust (likely HL rounding artifacts).
        if close_exchange_orphans:
            # Recompute from what is STILL open. `db_by_symbol` is the
            # pre-close snapshot, so a symbol whose only row pass 1 just
            # wrong-side-closed would be skipped here for one more cycle
            # — leaving an untracked exchange position that the next
            # strategy OPEN nets against.
            if closed_symbols:
                db_symbols = {p.symbol for p in await self._open_position_rows()}
            else:
                db_symbols = set(db_by_symbol.keys())
            orphans = [
                (sym, p) for sym, p in ex_by_symbol.items()
                if sym not in db_symbols and p.size >= 1e-6
            ]
            n_traded = sum(sym in traded_symbols for sym, _ in orphans)
            reasons = {
                sym: _orphan_hold_reason(sym, n_traded, hold_orphans, traded_symbols)
                for sym, _ in orphans
            }
            # A close needs a second read with the same orphans, each one
            # to close on the same side and size: a read can miss one, and
            # a human may cut or reverse one in between (NU-2). Otherwise it
            # waits for the next pass — deferred, not held: a blip sets no hold.
            result.unresolved_orphans = [s for s, _ in orphans if not reasons[s]]
            confirmed = not result.unresolved_orphans or await self._same_orphans(
                exchange, db_symbols,
                {s: None if reasons[s] else p for s, p in orphans},
                confirm_delay_seconds,
            )
            for sym, ex_pos in orphans:
                why = reasons[sym] or (
                    None if confirmed else "a re-read did not confirm the orphans"
                )
                if why:
                    msg = (
                        f"HELD exchange-orphan {ex_pos.side} {ex_pos.size} "
                        f"{sym} — not closed: {why}"
                    )
                    result.failures.append(msg)
                    if reasons[sym]:
                        result.held_orphans.append(sym)
                    logger.warning("Reconcile: %s", msg)
                    continue
                close_side = "buy" if ex_pos.side == "short" else "sell"
                try:
                    # Reduce-only (NU-5a): if the position changed since
                    # the read, this can shrink it and nothing else.
                    order = await exchange.place_order(
                        sym, close_side, ex_pos.size, OrderType.MARKET,
                        reduce_only=True,
                    )
                except Exception as e:
                    msg = (
                        f"FAILED to close exchange-orphan {ex_pos.side} "
                        f"{ex_pos.size} {sym}: {e}"
                    )
                    result.failures.append(msg)
                    logger.exception(
                        "Reconcile: failed to close exchange-orphan %s %s %s",
                        ex_pos.side, ex_pos.size, sym,
                    )
                    continue

                # A rejected close is not an action — the position is
                # still there. Reporting it as "closed" is how two live
                # REJECTEDs got counted as cleanups.
                if order.status != OrderStatus.FILLED:
                    msg = (
                        f"FAILED to close exchange-orphan {ex_pos.side} "
                        f"{ex_pos.size} {sym}: order status "
                        f"{order.status.value} — position still open"
                    )
                    result.failures.append(msg)
                    logger.error("Reconcile: %s", msg)
                    continue

                result.unresolved_orphans.remove(sym)
                fill_price = float(order.filled_price or ex_pos.entry_price or 0.0)
                # Reduce-only: a position cut since the read fills less,
                # and what filled is what is booked and reported.
                filled = float(order.size)
                fee = fill_price * filled * float(settings.taker_fee_rate)
                try:
                    await self.record_trade(
                        order_id=order.id,
                        strategy_name="reconcile",
                        symbol=sym,
                        side=close_side,
                        size=filled,
                        price=fill_price,
                        fee=fee,
                        # No DB row means no entry price, so there is no
                        # honest PnL to record. NULL, not 0.0.
                        pnl=None,
                        reason="reconcile: exchange-orphan close",
                    )
                except Exception as e:
                    # The close DID happen, so it stays an action — but
                    # an unbookkept fill is exactly the divergence this
                    # whole PR exists to stop, so it is also a failure
                    # the operator must see, with the order id to
                    # reconcile by hand.
                    fail = (
                        f"UNBOOKKEPT exchange-orphan close on {sym} "
                        f"(order_id={order.id}, {ex_pos.side} {filled} "
                        f"@ {fill_price:.6f}): Trade row failed to write: {e}"
                    )
                    result.failures.append(fail)
                    logger.exception(
                        "Reconcile: exchange-orphan closed on %s but the Trade "
                        "row failed to write (order_id=%s)", sym, order.id,
                    )
                msg = (
                    f"closed exchange-orphan: {ex_pos.side} {filled} "
                    f"{sym} @ {fill_price:.6f} (filled"
                    + (f" of {ex_pos.size})" if filled < ex_pos.size - 1e-12 else ")")
                )
                result.actions.append(msg)
                logger.warning("Reconcile: %s", msg)

        # --- 7. Always say the pass ran. -------------------------------
        if result.actions or result.failures:
            logger.info(
                "Reconcile: %d action(s), %d failure(s) — %d db rows, "
                "%d exchange positions",
                len(result.actions), len(result.failures),
                result.db_open, result.exchange_open,
            )
        else:
            logger.info(
                "Reconcile: clean — %d db rows, %d exchange positions",
                result.db_open, result.exchange_open,
            )
        return result

    @staticmethod
    async def _same_orphans(exchange, db_symbols, orphans, delay) -> bool:
        """True when a re-read after `delay` shows exactly the symbols of
        `orphans`, and each one to close (symbol -> Position; None when
        held for its own reason) on the same side and, within dust, the
        same size."""
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            again = await exchange.get_positions()
        except Exception:
            logger.warning("Reconcile: orphan re-read failed", exc_info=True)
            return False
        now = {
            p.symbol: p for p in again
            if p.symbol not in db_symbols and p.size >= 1e-6
        }
        return now.keys() == orphans.keys() and all(
            now[sym].side == p.side and abs(now[sym].size - p.size) < 1e-6
            for sym, p in orphans.items() if p
        )

    async def _insert_reconcile_trade(
        self,
        session: AsyncSession,
        *,
        preferred_order_id: str | None,
        strategy_name: str,
        symbol: str,
        side: str,
        size: float,
        price: float,
        fee: float,
        pnl: float | None,
        reason: str,
        timestamp: datetime,
    ) -> None:
        """Insert the `Trade` row for a reconcile close, in `session`.

        `Trade.order_id` is UNIQUE and one exchange fill can close two DB
        rows (two strategies on one coin net to a single HL position — the
        analysis found 19 timestamps carrying 2-3 rows). So the fill's oid
        is attempted inside a SAVEPOINT and a `reconcile-<uuid4>` is used
        when it is already taken, instead of the IntegrityError taking the
        whole reconcile transaction down with it.
        """
        candidates = [
            c for c in (preferred_order_id, f"reconcile-{uuid.uuid4()}") if c
        ]
        for order_id in candidates:
            trade = Trade(
                tenant_id=self._tenant_id,
                order_id=order_id,
                strategy_name=strategy_name,
                symbol=symbol,
                side=side,
                size=size,
                price=price,
                fee=fee,
                pnl=pnl,
                reason=reason,
                is_paper=self._is_paper,
                mode=self._mode,
                timestamp=timestamp,
            )
            try:
                async with session.begin_nested():
                    session.add(trade)
                    await session.flush()
                return
            except IntegrityError:
                logger.warning(
                    "Reconcile: order_id %s already recorded — retrying "
                    "the Trade row with a synthetic id", order_id,
                )
        logger.error(
            "Reconcile: could not write a Trade row for %s %s (%s)",
            strategy_name, symbol, reason,
        )

    # ------------------------------------------------------------------
    # HODL: manual on-chain levels + spot accumulation purchases
    # ------------------------------------------------------------------

    async def latest_onchain_level(self) -> ManualOnchainLevel | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(ManualOnchainLevel)
                .order_by(ManualOnchainLevel.recorded_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def record_onchain_level(
        self,
        sth_cost_basis_usd: float | None = None,
        lth_cost_basis_usd: float | None = None,
        realized_price_usd: float | None = None,
        cvdd_usd: float | None = None,
        source: str = "roots_newsletter",
        notes: str = "",
    ) -> int:
        async with self._session_factory() as session:
            row = ManualOnchainLevel(
                sth_cost_basis_usd=sth_cost_basis_usd,
                lth_cost_basis_usd=lth_cost_basis_usd,
                realized_price_usd=realized_price_usd,
                cvdd_usd=cvdd_usd,
                source=source,
                notes=notes,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def record_hodl_purchase(
        self,
        amount_local: float,
        btc_amount: float,
        btc_price_usd: float,
        local_currency: str = "SEK",
        btc_price_local: float | None = None,
        fx_rate: float | None = None,
        zone: str | None = None,
        exchange: str = "kraken",
        notes: str = "",
    ) -> int:
        async with self._session_factory() as session:
            row = HodlPurchase(
                amount_local=amount_local,
                local_currency=local_currency,
                btc_amount=btc_amount,
                btc_price_usd=btc_price_usd,
                btc_price_local=btc_price_local,
                fx_rate=fx_rate,
                zone=zone,
                exchange=exchange,
                notes=notes,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def mark_hodl_purchase_cold(
        self, purchase_id: int, address: str | None = None
    ) -> bool:
        async with self._session_factory() as session:
            row = await session.get(HodlPurchase, purchase_id)
            if row is None:
                return False
            row.cold_storage_at = datetime.now(timezone.utc)
            if address:
                row.cold_storage_address = address
            await session.commit()
            return True

    async def list_hodl_purchases(self, limit: int = 50) -> list[HodlPurchase]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(HodlPurchase)
                .order_by(HodlPurchase.purchased_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Vault scanner: catalog + daily snapshots + NAV history
    # ------------------------------------------------------------------

    async def upsert_vault(
        self,
        address: str,
        name: str,
        leader_address: str,
        description: str,
        created_at: datetime | None,
        profit_share_pct: float,
        relationship_type: str = "normal",
    ) -> None:
        """Insert or update vault metadata.

        `created_at` is allowed to be None when the vault is discovered
        through user-position tracking (we have no `createTimeMillis`
        without the catalogue scan); the next daily scan fills it in.
        """
        async with self._session_factory() as session:
            row = await session.get(Vault, address)
            if row is None:
                row = Vault(address=address, first_seen_at=datetime.now(timezone.utc))
                session.add(row)
            row.name = name
            row.leader_address = leader_address
            row.description = description
            # Don't overwrite a known created_at with None — the catalog
            # scan is authoritative once we've seen it.
            if created_at is not None or row.created_at is None:
                row.created_at = created_at
            row.profit_share_pct = profit_share_pct
            row.relationship_type = relationship_type
            await session.commit()

    async def append_nav_history(
        self,
        vault_address: str,
        points: list[tuple[datetime, float, float | None]],
    ) -> int:
        """Insert or backfill (timestamp, nav, pnl_cum) tuples on the
        composite PK. Returns count of newly inserted rows (backfills
        don't count). Caller passes 3-tuples; legacy 2-tuples default
        pnl_cum to None.

        Backfill rule: existing rows with `pnl_cum IS NULL` get updated
        when incoming data has a non-None pnl_cum. Without this, the
        first daily poll after a schema bump leaves yesterday's rows
        permanently unaware of pnl, which would force the metrics layer
        into the legacy NAV-delta fallback for the rest of the window.
        """
        if not points:
            return 0
        async with self._session_factory() as session:
            existing = await session.execute(
                select(
                    VaultNavPoint.timestamp, VaultNavPoint.pnl_cum
                ).where(
                    VaultNavPoint.vault_address == vault_address
                )
            )
            existing_pnl: dict = {ts: pnl for (ts, pnl) in existing.all()}
            inserted = 0
            backfilled = 0
            for point in points:
                if len(point) == 2:
                    ts, nav = point
                    pnl_cum = None
                else:
                    ts, nav, pnl_cum = point
                if ts in existing_pnl:
                    if existing_pnl[ts] is None and pnl_cum is not None:
                        # Backfill the pnl_cum on a previously-known timestamp.
                        await session.execute(
                            VaultNavPoint.__table__.update()
                            .where(VaultNavPoint.vault_address == vault_address)
                            .where(VaultNavPoint.timestamp == ts)
                            .values(pnl_cum=pnl_cum)
                        )
                        backfilled += 1
                    continue
                session.add(
                    VaultNavPoint(
                        vault_address=vault_address,
                        timestamp=ts,
                        nav=nav,
                        pnl_cum=pnl_cum,
                    )
                )
                inserted += 1
            if inserted or backfilled:
                await session.commit()
            return inserted

    async def save_vault_snapshot(
        self,
        vault_address: str,
        snapshot_at: datetime,
        aum_usd: float | None,
        nav: float | None,
        leader_equity_pct: float | None,
        depositor_count: int | None,
        apr: float | None,
        age_days: int | None,
        roi_7d: float | None,
        roi_30d: float | None,
        roi_90d: float | None,
        roi_180d: float | None,
        roi_365d: float | None,
        max_drawdown_pct: float | None,
        sharpe_180d: float | None,
        qualified: bool,
        filter_breakdown_json: str,
        allow_deposits: bool,
        is_closed: bool,
    ) -> int:
        async with self._session_factory() as session:
            existing = await session.execute(
                select(VaultSnapshot).where(
                    VaultSnapshot.vault_address == vault_address,
                    VaultSnapshot.snapshot_at == snapshot_at,
                )
            )
            row = existing.scalar_one_or_none()
            if row is None:
                row = VaultSnapshot(
                    vault_address=vault_address,
                    snapshot_at=snapshot_at,
                )
                session.add(row)
            row.aum_usd = aum_usd
            row.nav = nav
            row.leader_equity_pct = leader_equity_pct
            row.depositor_count = depositor_count
            row.apr = apr
            row.age_days = age_days
            row.roi_7d = roi_7d
            row.roi_30d = roi_30d
            row.roi_90d = roi_90d
            row.roi_180d = roi_180d
            row.roi_365d = roi_365d
            row.max_drawdown_pct = max_drawdown_pct
            row.sharpe_180d = sharpe_180d
            row.qualified = qualified
            row.filter_breakdown_json = filter_breakdown_json
            row.allow_deposits = allow_deposits
            row.is_closed = is_closed
            await session.commit()
            return int(row.id)

    async def latest_vault_snapshot(
        self, vault_address: str
    ) -> VaultSnapshot | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(VaultSnapshot)
                .where(VaultSnapshot.vault_address == vault_address)
                .order_by(VaultSnapshot.snapshot_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    async def latest_qualified_vaults(
        self, *, max_age_days: int = 7, limit: int = 200,
    ) -> list[tuple[Vault, VaultSnapshot]]:
        """Return currently qualified vaults, joined with their latest snapshot.

        "Latest snapshot" means the most recent row per vault — this gives
        us the current verdict, not historical state. We cap the staleness
        at `max_age_days` so a vault that hasn't been re-evaluated in over
        a week (scanner outage, mode switch, etc.) is treated as unknown
        rather than perpetually qualified. Default of 7 days survives a
        few missed daily polls without going silent.

        `limit` (audit M7): hard cap on returned rows. The full HL vault
        catalogue can grow into the thousands; an unbounded list pinned
        the event loop on JSON encoding for several hundred-ms hits, and
        repeated requests would amplify it. 200 covers the realistic
        qualified set with significant headroom.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        async with self._session_factory() as session:
            # Pick the newest snapshot per vault; filter to qualified later.
            # Sorting by (vault, snapshot_at desc) lets us pluck the head row
            # per vault in one pass.
            #
            # Stream via the scalars iterator (NOT .all()) so the early
            # `break` actually saves work — `.all()` would materialize
            # every snapshot row into memory before our break, defeating
            # the limit's whole point. Audit M7 / PR #21 review.
            # Long-term: push DISTINCT ON / window function + LIMIT into
            # SQL so the DB itself stops fetching at `limit` rows.
            result = await session.execute(
                select(VaultSnapshot)
                .order_by(
                    VaultSnapshot.vault_address,
                    VaultSnapshot.snapshot_at.desc(),
                )
            )
            seen: set[str] = set()
            picks: list[VaultSnapshot] = []
            for snap in result.scalars():
                if snap.vault_address in seen:
                    continue
                seen.add(snap.vault_address)
                if not snap.qualified:
                    continue
                if snap.snapshot_at and snap.snapshot_at < cutoff:
                    continue
                picks.append(snap)
                if len(picks) >= limit:
                    break
            out: list[tuple[Vault, VaultSnapshot]] = []
            for snap in picks:
                vault = await session.get(Vault, snap.vault_address)
                if vault is not None:
                    out.append((vault, snap))
            return out

    async def vault_snapshots_for(
        self, vault_address: str, limit: int = 90
    ) -> list[VaultSnapshot]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(VaultSnapshot)
                .where(VaultSnapshot.vault_address == vault_address)
                .order_by(VaultSnapshot.snapshot_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def vault_nav_for(
        self, vault_address: str
    ) -> list[VaultNavPoint]:
        """Return all stored NAV+PnL samples for a vault, oldest first.
        Includes `pnl_cum` so callers can compute flow-neutral returns."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(VaultNavPoint)
                .where(VaultNavPoint.vault_address == vault_address)
                .order_by(VaultNavPoint.timestamp.asc())
            )
            return list(result.scalars().all())

    async def get_vault(self, address: str) -> Vault | None:
        async with self._session_factory() as session:
            return await session.get(Vault, address)

    # ------------------------------------------------------------------
    # User vault positions: track which vaults the user holds + P&L
    # ------------------------------------------------------------------

    async def upsert_user_vault_entry(
        self,
        user_address: str,
        vault_address: str,
        vault_equity_usd: float,
        unrealized_pnl_usd: float,
        all_time_pnl_usd: float,
        days_following: int,
        entered_at: datetime | None,
        locked_until: datetime | None,
    ) -> None:
        """Update the user's stake in a vault from HL's followerState.
        Equity ≈ 0 (sub-$1 dust) marks exited; re-entry after exit clears
        the exited flag."""
        async with self._session_factory() as session:
            row = await session.get(
                UserVaultEntry, (user_address, vault_address)
            )
            now = datetime.now(timezone.utc)
            if row is None:
                row = UserVaultEntry(
                    user_address=user_address,
                    vault_address=vault_address,
                )
                session.add(row)
            elif row.exited_at is not None and vault_equity_usd > 1.0:
                # Re-entry after a withdraw — clear exit flag.
                row.exited_at = None
            row.vault_equity_usd = vault_equity_usd
            row.unrealized_pnl_usd = unrealized_pnl_usd
            row.all_time_pnl_usd = all_time_pnl_usd
            row.days_following = days_following
            row.entered_at = entered_at
            row.last_seen_at = now
            row.locked_until = locked_until
            if vault_equity_usd < 1.0 and row.exited_at is None:
                row.exited_at = now
            await session.commit()

    async def list_user_vault_entries(
        self, user_address: str, *, include_exited: bool = False
    ) -> list[UserVaultEntry]:
        async with self._session_factory() as session:
            stmt = select(UserVaultEntry).where(
                UserVaultEntry.user_address == user_address
            )
            if not include_exited:
                stmt = stmt.where(UserVaultEntry.exited_at.is_(None))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    # ----- Operator admin: per-tenant policy (alembic 0016) -----

    # ----- Telegram unlock-link flow (PR 3b) -----

    async def get_tenant_id_for_telegram_chat(
        self, chat_id: int
    ) -> uuid.UUID | None:
        """Lookup the linked tenant for a Telegram chat. Used by
        future /unlock command (PR 3c+) where we already know the
        chat but need the tenant. Returns None when unlinked.

        Schema guarantees 1:1 via UNIQUE index (alembic 0013), so
        this returns either zero or exactly one row — never an
        arbitrary pick from multiple."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(TenantTelegramLink.tenant_id).where(
                    TenantTelegramLink.telegram_chat_id == chat_id
                )
            )
            row = result.first()
            return row[0] if row else None

    async def upsert_telegram_link(
        self,
        tenant_id: uuid.UUID,
        telegram_chat_id: int,
        telegram_username: str | None,
    ) -> None:
        """Insert-or-update the (tenant_id, chat_id) pair. Called by
        the bot's /link handler after validating the 6-digit code
        against Redis.

        Semantics:
        - Re-linking the same tenant to a different chat overwrites
          their row (single-device beta UX).
        - Linking a chat already used by another tenant transfers
          the chat to the new tenant — the old tenant's row gets
          deleted to satisfy the UNIQUE constraint on chat_id
          (alembic 0013). This is intentional: if user A switched
          phones and inherited B's chat id, the most-recent /link
          wins. Both sides explicitly opted in by generating + using
          a code, so this isn't a stealth-hijack.
        """
        async with self._session_factory() as session:
            async with session.begin():
                # If the chat is already linked to a DIFFERENT
                # tenant, evict that row first so the UNIQUE
                # constraint on telegram_chat_id doesn't fire on
                # insert/update below.
                result = await session.execute(
                    select(TenantTelegramLink).where(
                        TenantTelegramLink.telegram_chat_id == telegram_chat_id,
                        TenantTelegramLink.tenant_id != tenant_id,
                    )
                )
                stale = result.scalars().first()
                if stale is not None:
                    await session.delete(stale)
                    await session.flush()

                existing = await session.get(TenantTelegramLink, tenant_id)
                if existing is None:
                    session.add(
                        TenantTelegramLink(
                            tenant_id=tenant_id,
                            telegram_chat_id=telegram_chat_id,
                            telegram_username=telegram_username,
                        )
                    )
                else:
                    existing.telegram_chat_id = telegram_chat_id
                    existing.telegram_username = telegram_username
                    existing.linked_at = datetime.now(timezone.utc)

    async def close(self) -> None:
        await self._engine.dispose()
