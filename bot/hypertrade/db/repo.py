"""Database repository for trades and positions."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
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
    Trade,
    Vault,
    VaultNavPoint,
    VaultSnapshot,
)

logger = logging.getLogger(__name__)


class Repository:
    def __init__(self, database_url: str | None = None) -> None:
        url = database_url or settings.database_url
        self._engine = create_async_engine(url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._mode = settings.exchange_mode
        self._is_paper = settings.is_paper

    async def init_db(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ready")

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
        created_at: datetime,
        profit_share_pct: float,
        relationship_type: str = "normal",
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(Vault, address)
            if row is None:
                row = Vault(address=address, first_seen_at=datetime.now(timezone.utc))
                session.add(row)
            row.name = name
            row.leader_address = leader_address
            row.description = description
            row.created_at = created_at
            row.profit_share_pct = profit_share_pct
            row.relationship_type = relationship_type
            await session.commit()

    async def append_nav_history(
        self, vault_address: str, points: list[tuple[datetime, float]]
    ) -> int:
        """Insert NAV points, skipping duplicates on the composite PK.
        Returns count of newly inserted rows."""
        if not points:
            return 0
        async with self._session_factory() as session:
            existing = await session.execute(
                select(VaultNavPoint.timestamp).where(
                    VaultNavPoint.vault_address == vault_address
                )
            )
            seen = {ts for (ts,) in existing.all()}
            inserted = 0
            for ts, nav in points:
                if ts in seen:
                    continue
                session.add(
                    VaultNavPoint(vault_address=vault_address, timestamp=ts, nav=nav)
                )
                inserted += 1
            if inserted:
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

    async def latest_qualified_vaults(self) -> list[tuple[Vault, VaultSnapshot]]:
        """Return current qualified vaults, joined with their latest snapshot.

        "Latest snapshot" means the most recent row per vault — this gives
        us the current verdict, not historical state.
        """
        async with self._session_factory() as session:
            # Pull all snapshots from last 48h, pick newest per vault, filter qualified.
            cutoff = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=48)
            result = await session.execute(
                select(VaultSnapshot)
                .where(VaultSnapshot.snapshot_at >= cutoff)
                .order_by(VaultSnapshot.vault_address, VaultSnapshot.snapshot_at.desc())
            )
            seen: set[str] = set()
            picks: list[VaultSnapshot] = []
            for snap in result.scalars().all():
                if snap.vault_address in seen:
                    continue
                seen.add(snap.vault_address)
                if snap.qualified:
                    picks.append(snap)
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

    async def close(self) -> None:
        await self._engine.dispose()
