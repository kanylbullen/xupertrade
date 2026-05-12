"""Database repository for trades and positions."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
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

logger = logging.getLogger(__name__)


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
    ) -> Trade:
        """Insert Trade + UPDATE matching open PositionRecord atomically.

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
                if pos is not None:
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
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord)
                .where(
                    PositionRecord.symbol == symbol,
                    PositionRecord.mode == self._mode,
                    PositionRecord.is_open == True,
                )
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

    async def reconcile_positions(
        self, exchange, close_exchange_orphans: bool = True,
        on_strategy_close=None,
    ) -> list[str]:
        """Compare DB open positions vs exchange reality:

        1. DB open + exchange has no position → close DB row (orphan)
        2. DB side ≠ exchange side → close DB row (wrong-side)
        3. Exchange has position + DB has no row → market-close on exchange
           (exchange-side orphan). Untracked positions are dangerous: a
           strategy's next OPEN will net against them, producing more
           DB-vs-exchange divergence. Set close_exchange_orphans=False if
           you want this only logged.
        4. Same-side size mismatch → logged only (ambiguous attribution).

        Returns a list of human-readable actions taken.
        """
        actions: list[str] = []
        try:
            ex_positions = await exchange.get_positions()
        except Exception as e:
            logger.warning("Reconcile: failed to fetch exchange positions: %s", e)
            return actions

        ex_by_symbol = {p.symbol: p for p in ex_positions}

        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.is_open == True,
                    PositionRecord.mode == self._mode,
                )
            )
            db_positions = list(result.scalars().all())

            db_by_symbol: dict[str, list[PositionRecord]] = {}
            for p in db_positions:
                db_by_symbol.setdefault(p.symbol, []).append(p)

            now = datetime.now(timezone.utc)

            for symbol, db_pos_list in db_by_symbol.items():
                ex_pos = ex_by_symbol.get(symbol)

                if ex_pos is None:
                    for p in db_pos_list:
                        p.is_open = False
                        p.exit_price = p.entry_price
                        p.pnl = 0.0
                        p.closed_at = now
                        msg = (
                            f"closed orphan: {p.strategy_name} "
                            f"{p.side} {p.size} {p.symbol} "
                            f"(no exchange position)"
                        )
                        actions.append(msg)
                        logger.warning("Reconcile: %s", msg)
                        if on_strategy_close:
                            on_strategy_close(p.strategy_name)
                    continue

                for p in db_pos_list:
                    if p.side != ex_pos.side:
                        p.is_open = False
                        p.exit_price = p.entry_price
                        p.pnl = 0.0
                        p.closed_at = now
                        msg = (
                            f"closed wrong-side: {p.strategy_name} "
                            f"{p.side} {p.symbol} (exchange has {ex_pos.side})"
                        )
                        actions.append(msg)
                        logger.warning("Reconcile: %s", msg)
                        if on_strategy_close:
                            on_strategy_close(p.strategy_name)

                # Size mismatch detection. Only flag if the diff is more
                # than 0.5% of the position OR more than 0.1% absolute —
                # smaller diffs are normal exchange-side rounding.
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

            await session.commit()

        # Pass 2: exchange has positions that no DB row covers → close them.
        # Done outside the session because we need to call exchange.place_order.
        # Threshold: ignore tiny dust positions (likely HL rounding artifacts).
        if close_exchange_orphans:
            db_symbols = set(db_by_symbol.keys())
            for sym, ex_pos in ex_by_symbol.items():
                if sym in db_symbols:
                    continue  # has DB tracking, handled above
                if ex_pos.size < 1e-6:
                    continue
                # Close it via a market order in the opposite direction
                from hypertrade.exchange.base import OrderType
                close_side = "buy" if ex_pos.side == "short" else "sell"
                try:
                    order = await exchange.place_order(
                        sym, close_side, ex_pos.size, OrderType.MARKET
                    )
                    msg = (
                        f"closed exchange-orphan: {ex_pos.side} {ex_pos.size} "
                        f"{sym} @ ~{ex_pos.entry_price} → fill status "
                        f"{order.status.value}"
                    )
                    actions.append(msg)
                    logger.warning("Reconcile: %s", msg)
                except Exception:
                    logger.exception(
                        "Reconcile: failed to close exchange-orphan %s %s %s",
                        ex_pos.side, ex_pos.size, sym,
                    )

        return actions

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
