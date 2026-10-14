"""Backfill funding_payments from HyperLiquid (roadmap NU-6.1).

Reads the account's `userFunding` history from --since, paged and
attributed as the poller does (`engine/funding.py`), and reports the
total, what is stored and what is missing. Only --apply writes, and a
rerun writes nothing twice. Run it in the container of the bot whose
funding it is: that environment's DATABASE_URL, TENANT_ID,
EXCHANGE_MODE (which --mode must name) and HL account are the ones used.

    python -m hypertrade.reports.funding_backfill --since 2026-04-28 --mode testnet
    python -m hypertrade.reports.funding_backfill --since 2026-04-28 --mode testnet --apply
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import sys
from datetime import datetime, timezone

from hypertrade.config import settings
from hypertrade.db.repo import Repository
from hypertrade.engine import funding
from hypertrade.exchange.hyperliquid import HyperLiquidExchange

logger = logging.getLogger(__name__)

# A full page weighs 45 of the IP's 1200 per minute, which every bot on
# the host shares: 15 s between pages keeps a backfill near 180/min.
DEFAULT_PAGE_PAUSE_SECONDS = 15.0


def _utc_day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from exc


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m hypertrade.reports.funding_backfill")
    p.add_argument("--since", type=_utc_day, required=True, help="first UTC day")
    p.add_argument("--mode", choices=("testnet", "mainnet"), required=True,
                   help="must be this environment's EXCHANGE_MODE")
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--page-pause", type=float, default=DEFAULT_PAGE_PAUSE_SECONDS)
    return p.parse_args(argv)


def format_report(result: funding.IngestResult, args: argparse.Namespace) -> str:
    verb = "inserted" if args.apply else "missing (not written: dry run)"
    lines = [
        f"Funding backfill: {args.mode}, {args.since.date().isoformat()} to now",
        f"  pages read       {result.pages}",
        f"  events read      {result.events}   total {result.total_usdc:+.4f} USDC",
        f"  already stored   {result.events - result.new}",
        f"  {verb}: {result.new}   {result.new_usdc:+.4f} USDC, "
        f"{result.unattributed_new} with no position",
    ]
    if result.skipped:
        lines.append(f"  unreadable records skipped: {result.skipped}")
    if result.by_coin:
        lines.append("  per coin, all events:")
        for coin, usdc in sorted(result.by_coin.items()):
            lines.append(f"    {coin:<10} {usdc:+.4f}")
    if result.by_strategy:
        lines.append(f"  per strategy, {'inserted' if args.apply else 'missing'} events:")
        for name, usdc in sorted(result.by_strategy.items(), key=lambda kv: kv[0] or ""):
            lines.append(f"    {name or '(no position)':<24} {usdc:+.4f}")
    if not result.complete:
        where = result.newest or args.since
        lines.append(f"  INCOMPLETE: stopped after {where}; rerun, what is stored is skipped")
    return "\n".join(lines)


async def run(
    argv: list[str] | None = None,
    *,
    fetch: funding.FetchFunding | None = None,
    repo: Repository | None = None,
) -> int:
    """`fetch` and `repo` are for tests; the CLI builds its own."""
    args = parse_args(argv)
    if args.mode != settings.exchange_mode:
        print(
            f"this environment is EXCHANGE_MODE={settings.exchange_mode}: run "
            f"in the {args.mode} bot's container",
            file=sys.stderr,
        )
        return 2
    if repo is None and not settings.tenant_id:
        print("no tenant: TENANT_ID is not set", file=sys.stderr)
        return 2
    if fetch is None:
        # The bot's own exchange wrapper, account and read retry; strict,
        # so a failed read ends the run instead of looking like the end.
        fetch = functools.partial(
            HyperLiquidExchange().get_user_funding_history, strict=True,
        )
    own_repo = repo is None
    if own_repo:
        repo = Repository(tenant_id=settings.tenant_id)
    result = funding.IngestResult()
    try:
        await funding.ingest_funding(
            repo, fetch, funding.to_ms(args.since),
            apply=args.apply, page_pause=args.page_pause, result=result,
        )
    except Exception:  # noqa: BLE001 — reported, with what was done
        logger.exception("Funding backfill stopped early")
        result.complete = False
    finally:
        if own_repo:
            await repo.close()
    print(format_report(result, args))
    return 0 if result.complete else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
