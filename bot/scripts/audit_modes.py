"""Audit cross-mode contamination from the pre-fix repo bug.

Before the fix, repo.close_position() did a SELECT without a mode filter, so a
testnet bot could mutate a paper PositionRecord (or vice versa) when its own
mode had no matching open row. This script reports — but does NOT modify —
suspicious rows so you can decide what to do.

Run inside the bot container:
    docker exec -it hypertrade-bot-paper python -m scripts.audit_modes

Or locally if DATABASE_URL is set in your env.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from hypertrade.db.models import PositionRecord, Trade
from hypertrade.db.repo import Repository


CLOSE_SIDE = {"long": "sell", "short": "buy"}


async def main() -> None:
    repo = Repository()
    async with repo._session_factory() as s:
        open_rows = (
            await s.execute(
                select(PositionRecord).where(PositionRecord.is_open.is_(True))
            )
        ).scalars().all()

        print("=== Open positions per mode ===")
        by_mode: dict[str, list[PositionRecord]] = {}
        for p in open_rows:
            by_mode.setdefault(p.mode, []).append(p)
        for mode, rows in sorted(by_mode.items()):
            print(f"  {mode}: {len(rows)}")
            for p in rows:
                print(
                    f"    id={p.id} {p.strategy_name} {p.side} {p.size} "
                    f"{p.symbol} @ {p.entry_price} opened={p.opened_at}"
                )

        dup_keys: dict[tuple[str, str], list[PositionRecord]] = {}
        for p in open_rows:
            dup_keys.setdefault((p.strategy_name, p.symbol), []).append(p)
        multi = {k: v for k, v in dup_keys.items() if len(v) > 1}
        if multi:
            print("\n=== Same (strategy, symbol) open in multiple modes ===")
            for (strat, sym), rows in multi.items():
                modes = ", ".join(f"{r.mode}#{r.id}" for r in rows)
                print(f"  {strat} {sym}: {modes}")
            print(
                "  (After the fix this is fine — each mode is isolated. Before the fix "
                "the next close_position call would have raised MultipleResultsFound.)"
            )

        closed_rows = (
            await s.execute(
                select(PositionRecord)
                .where(PositionRecord.is_open.is_(False))
                .where(PositionRecord.closed_at.isnot(None))
                .order_by(PositionRecord.closed_at.desc())
            )
        ).scalars().all()

        print(f"\n=== Suspicious closed positions ({len(closed_rows)} total closed) ===")
        suspects = []
        for p in closed_rows:
            window_lo = p.closed_at - timedelta(minutes=2)
            window_hi = p.closed_at + timedelta(minutes=2)
            expected_side = CLOSE_SIDE.get(p.side, "")
            matching_trade = (
                await s.execute(
                    select(Trade)
                    .where(
                        Trade.strategy_name == p.strategy_name,
                        Trade.symbol == p.symbol,
                        Trade.side == expected_side,
                        Trade.mode == p.mode,
                        Trade.timestamp >= window_lo,
                        Trade.timestamp <= window_hi,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

            if matching_trade is None:
                cross_trade = (
                    await s.execute(
                        select(Trade)
                        .where(
                            Trade.strategy_name == p.strategy_name,
                            Trade.symbol == p.symbol,
                            Trade.side == expected_side,
                            Trade.mode != p.mode,
                            Trade.timestamp >= window_lo,
                            Trade.timestamp <= window_hi,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                suspects.append((p, cross_trade))

        if not suspects:
            print("  None — every closed PositionRecord has a same-mode closing Trade nearby.")
        else:
            for p, cross in suspects:
                cross_str = (
                    f"  → likely closed by mode={cross.mode} "
                    f"(trade id={cross.id} ts={cross.timestamp})"
                    if cross
                    else "  → no close-side Trade found in any mode (orphan)"
                )
                print(
                    f"  pos id={p.id} mode={p.mode} {p.strategy_name} {p.side} "
                    f"{p.symbol} closed={p.closed_at} pnl={p.pnl}\n{cross_str}"
                )

    await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
