"""Record a manual on-chain level snapshot.

Run weekly after reading Roots' newsletter:

    cd bot && uv run python -m scripts.record_levels \\
        --sth 81000 --realized 54300 --cvdd 44200 --lth 45400 \\
        --notes "from Roots weekly 2026-05-04"

All numeric flags are optional — provide whichever values are in the
newsletter that week. Stored in manual_onchain_levels table; the
btc_accumulation_zone HODL signal uses the most recent row when ≤14
days old, otherwise falls back to SMA proxies.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hypertrade.db.repo import Repository


async def main() -> int:
    parser = argparse.ArgumentParser(description="Record manual on-chain levels")
    parser.add_argument("--sth", type=float, help="STH cost basis (USD)")
    parser.add_argument("--realized", type=float, help="Realized Price (USD)")
    parser.add_argument("--cvdd", type=float, help="CVDD (USD)")
    parser.add_argument("--lth", type=float, help="LTH cost basis (USD)")
    parser.add_argument("--source", default="roots_newsletter")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    if not any([args.sth, args.realized, args.cvdd, args.lth]):
        print("error: provide at least one of --sth/--realized/--cvdd/--lth",
              file=sys.stderr)
        return 2

    repo = Repository()
    try:
        rid = await repo.record_onchain_level(
            sth_cost_basis_usd=args.sth,
            lth_cost_basis_usd=args.lth,
            realized_price_usd=args.realized,
            cvdd_usd=args.cvdd,
            source=args.source,
            notes=args.notes,
        )
    finally:
        await repo.close()

    parts = []
    if args.sth: parts.append(f"STH=${args.sth:,.0f}")
    if args.realized: parts.append(f"RP=${args.realized:,.0f}")
    if args.cvdd: parts.append(f"CVDD=${args.cvdd:,.0f}")
    if args.lth: parts.append(f"LTH=${args.lth:,.0f}")
    print(f"Recorded level #{rid}: {', '.join(parts)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
