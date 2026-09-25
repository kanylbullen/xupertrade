"""Backfill funding_payments from HyperLiquid (roadmap NU-6.1).

Reads the account's `userFunding` history from --since, paged and
attributed as the poller does (`engine/funding.py`), and reports the
total, what is stored and what is missing. Only --apply writes, and a
rerun writes nothing twice. Run it in the bot's environment
(DATABASE_URL, TENANT_ID, the HL account):

    python -m hypertrade.reports.funding_backfill --since 2026-04-28 --mode testnet
    python -m hypertrade.reports.funding_backfill --since 2026-04-28 --mode testnet --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from hyperliquid.api import API
from hyperliquid.utils import constants

from hypertrade.config import settings
from hypertrade.db.repo import Repository
from hypertrade.engine import funding

# A full page weighs 45 of the IP's 1200 per minute, which every bot on
# the host shares: 15 s between pages keeps a backfill near 180/min.
DEFAULT_PAGE_PAUSE_SECONDS = 15.0
_FETCH_ATTEMPTS = 3
_FETCH_TIMEOUT_SECONDS = 30.0


def _utc_day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from exc


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m hypertrade.reports.funding_backfill")
    p.add_argument("--since", type=_utc_day, required=True, help="first UTC day")
    p.add_argument("--until", type=_utc_day, help="UTC day to stop before (default: now)")
    p.add_argument("--mode", choices=("testnet", "mainnet"), required=True)
    p.add_argument("--apply", action="store_true", help="write (default: dry run)")
    p.add_argument("--tenant-id", help="default: TENANT_ID")
    p.add_argument("--address", help="HL account (default: this environment's)")
    p.add_argument("--page-pause", type=float, default=DEFAULT_PAGE_PAUSE_SECONDS)
    return p.parse_args(argv)


def resolve_address(args: argparse.Namespace) -> str:
    """The HL account to read. The environment's account belongs to the
    environment's mode, so it is used only when that is --mode."""
    if args.address:
        return args.address.strip()
    if settings.exchange_mode != args.mode:
        raise SystemExit(
            f"this environment is EXCHANGE_MODE={settings.exchange_mode}, "
            f"so its account is not the {args.mode} one: run in the "
            f"{args.mode} bot's container, or pass --address"
        )
    if settings.hyperliquid_account_address.strip():
        return settings.hyperliquid_account_address.strip()
    if settings.hyperliquid_private_key:
        from eth_account import Account  # noqa: PLC0415 — only this path
        return Account.from_key(settings.hyperliquid_private_key).address
    raise SystemExit("no HL account: set HYPERLIQUID_ACCOUNT_ADDRESS or pass --address")


def hl_fetcher(base_url: str, address: str) -> funding.FetchFunding:
    """`userFunding` pages for `address`. Unlike the exchange wrapper's
    read, a failure raises instead of answering [] — a backfill that
    stopped early must not look finished."""
    api = API(base_url, timeout=_FETCH_TIMEOUT_SECONDS)

    async def fetch(start_ms: int, end_ms: int | None) -> list[dict]:
        payload: dict = {"type": "userFunding", "user": address, "startTime": start_ms}
        if end_ms is not None:
            payload["endTime"] = end_ms
        loop = asyncio.get_running_loop()
        last: BaseException | None = None
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                page = await loop.run_in_executor(None, api.post, "/info", payload)
            except Exception as exc:  # noqa: BLE001 — retried, then raised
                last = exc
            else:
                if isinstance(page, list):
                    return page
                last = RuntimeError(f"userFunding answered {str(page)[:200]}")
            if attempt + 1 < _FETCH_ATTEMPTS:
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(
            f"userFunding failed {_FETCH_ATTEMPTS} times from {start_ms}"
        ) from last

    return fetch


def format_report(result: funding.IngestResult, args: argparse.Namespace) -> str:
    until = args.until.date().isoformat() if args.until else "now"
    verb = "inserted" if args.apply else "missing (not written: dry run)"
    lines = [
        f"Funding backfill: {args.mode}, {args.since.date().isoformat()} to {until}",
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
        lines.append(f"  INCOMPLETE: paging stopped at {result.newest}; rerun from there")
    return "\n".join(lines)


async def run(
    argv: list[str] | None = None,
    *,
    fetch: funding.FetchFunding | None = None,
    repo: Repository | None = None,
) -> int:
    """`fetch` and `repo` are for tests; the CLI builds its own."""
    args = parse_args(argv)
    tenant_id = args.tenant_id or settings.tenant_id
    if repo is None and not tenant_id:
        print("no tenant: set TENANT_ID or pass --tenant-id", file=sys.stderr)
        return 2
    if fetch is None:
        base_url = (
            constants.TESTNET_API_URL if args.mode == "testnet"
            else constants.MAINNET_API_URL
        )
        fetch = hl_fetcher(base_url, resolve_address(args))
    own_repo = repo is None
    if own_repo:
        repo = Repository(tenant_id=tenant_id, mode=args.mode)
    try:
        result = await funding.ingest_funding(
            repo,
            fetch,
            funding.to_ms(args.since),
            # HL's endTime is inclusive; --until is the day to stop before.
            funding.to_ms(args.until) - 1 if args.until else None,
            apply=args.apply,
            page_pause=args.page_pause,
        )
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
