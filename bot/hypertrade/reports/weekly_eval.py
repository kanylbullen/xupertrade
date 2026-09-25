"""Weekly per-strategy evaluation.

Aggregates trade history over a window (default 7 days) and produces
a summary per strategy: trade count, win rate, realized PnL, avg PnL,
max consecutive loss, and a flag for strategies that have been silent.

Usage as a script:
    cd bot && uv run python -m hypertrade.reports.weekly_eval [--days 7]

Usage from code:
    summary = await evaluate(repo, strategies, days=7)
    text = format_summary_text(summary, days=7)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from hypertrade.config import settings
from hypertrade.db.repo import Repository
from hypertrade.strategies.registry import list_strategies, load_all

logger = logging.getLogger(__name__)


@dataclass
class StrategyStats:
    name: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    pnls: list[float] = field(default_factory=list)
    last_trade: datetime | None = None
    days_silent: int | None = None  # None = no trade history at all

    @property
    def win_rate(self) -> float:
        decisive = self.wins + self.losses
        return (self.wins / decisive) if decisive > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return (self.realized_pnl / len(self.pnls)) if self.pnls else 0.0

    @property
    def max_consec_loss(self) -> int:
        run = best = 0
        for p in self.pnls:
            if p < 0:
                run += 1
                best = max(best, run)
            else:
                run = 0
        return best


async def evaluate(
    repo: Repository,
    strategy_names: list[str],
    days: int = 7,
) -> dict[str, StrategyStats]:
    """Aggregate per-strategy stats over the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    trades = await repo.get_trades_since(since)

    stats = {n: StrategyStats(name=n) for n in strategy_names}

    for t in trades:
        s = stats.get(t.strategy_name)
        if s is None:
            # Trade exists for a strategy not in the active list (legacy)
            s = stats.setdefault(t.strategy_name, StrategyStats(name=t.strategy_name))
        s.trades += 1
        s.fees += float(t.fee or 0)
        if t.timestamp and (s.last_trade is None or t.timestamp > s.last_trade):
            s.last_trade = t.timestamp
        if t.pnl is not None:
            pnl = float(t.pnl)
            s.realized_pnl += pnl
            s.pnls.append(pnl)
            if pnl > 0:
                s.wins += 1
            elif pnl < 0:
                s.losses += 1

    now = datetime.now(timezone.utc)
    for s in stats.values():
        if s.last_trade is not None:
            s.days_silent = (now - s.last_trade).days
        else:
            s.days_silent = None

    return stats


def format_summary_text(
    stats: dict[str, StrategyStats], days: int = 7, html: bool = False
) -> str:
    """Format the evaluation as plain text or Telegram HTML."""
    sorted_stats = sorted(stats.values(), key=lambda s: s.realized_pnl, reverse=True)

    bold = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    code = (lambda s: f"<code>{s}</code>") if html else (lambda s: s)

    lines = [
        f"📈 {bold(f'Strategy evaluation — last {days}d')}",
        f"Mode: {settings.exchange_mode}",
        "",
    ]

    total_pnl = sum(s.realized_pnl for s in stats.values())
    total_trades = sum(s.trades for s in stats.values())
    total_wins = sum(s.wins for s in stats.values())
    total_losses = sum(s.losses for s in stats.values())
    sign = "+" if total_pnl >= 0 else ""
    lines.append(
        f"Total: {sign}${total_pnl:,.2f} | "
        f"{total_trades} trades ({total_wins}W/{total_losses}L)"
    )
    lines.append("")

    silent = []
    for s in sorted_stats:
        if s.trades == 0:
            silent.append(s)
            continue
        sign = "+" if s.realized_pnl >= 0 else ""
        wr = f"{s.win_rate * 100:.0f}%" if (s.wins + s.losses) > 0 else "—"
        lines.append(
            f"{code(s.name)}: {sign}${s.realized_pnl:,.2f} "
            f"({s.trades}t, {wr} wr, max-loss-streak {s.max_consec_loss})"
        )

    if silent:
        lines.append("")
        lines.append(bold("Silent (no trades):"))
        for s in silent:
            silent_str = f" ({s.days_silent}d ago)" if s.days_silent is not None else " (never)"
            lines.append(f"  {code(s.name)}{silent_str}")

    flagged = [s for s in stats.values() if s.days_silent is not None and s.days_silent >= 14]
    if flagged:
        lines.append("")
        lines.append("⚠️ " + bold("Flag for review (silent ≥14d):"))
        for s in flagged:
            lines.append(f"  {code(s.name)} — silent {s.days_silent}d")

    return "\n".join(lines)


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    load_all()
    names = list_strategies()
    repo = Repository()
    try:
        stats = await evaluate(repo, names, days=args.days)
        print(format_summary_text(stats, days=args.days, html=False))
    finally:
        await repo.close()


if __name__ == "__main__":
    asyncio.run(_main())
