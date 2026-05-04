"""Record a HODL spot purchase.

Use after a manual buy on Kraken/Binance/etc:

    cd bot && uv run python -m scripts.record_purchase \\
        --amount-sek 5000 --btc 0.005 --btc-usd 80100 --usd-sek 10.5 \\
        --zone yellow --notes "weekly DCA + 50% kicker"

Use --mark-cold <id> [--address bc1...] later to mark a purchase as
moved off the exchange (for tax/anskaffningsvärde audit trail).

    cd bot && uv run python -m scripts.record_purchase \\
        --mark-cold 7 --address bc1qabc...
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hypertrade.db.repo import Repository


async def main() -> int:
    parser = argparse.ArgumentParser(description="Record a HODL purchase or mark cold storage")
    parser.add_argument("--amount-sek", type=float, help="Amount spent in SEK")
    parser.add_argument("--btc", type=float, help="BTC received")
    parser.add_argument("--btc-usd", type=float, help="BTC spot price in USD at purchase")
    parser.add_argument("--usd-sek", type=float,
                        help="USD/SEK rate (1 USD = X SEK). If omitted, derived from amount.")
    parser.add_argument("--exchange", default="kraken")
    parser.add_argument("--zone", choices=["green", "yellow", "red", "deep"],
                        help="Zone at the time of purchase (optional, for stats)")
    parser.add_argument("--notes", default="")
    parser.add_argument("--mark-cold", type=int, metavar="ID",
                        help="Mark an existing purchase as moved to cold storage")
    parser.add_argument("--address", help="Cold-storage address (used with --mark-cold)")
    args = parser.parse_args()

    repo = Repository()
    try:
        if args.mark_cold:
            ok = await repo.mark_hodl_purchase_cold(args.mark_cold, address=args.address)
            if ok:
                print(f"Marked purchase #{args.mark_cold} as cold-stored"
                      + (f" → {args.address}" if args.address else ""))
                return 0
            print(f"error: no purchase with id {args.mark_cold}", file=sys.stderr)
            return 1

        if not (args.amount_sek and args.btc and args.btc_usd):
            print("error: --amount-sek, --btc, --btc-usd are required for new purchases",
                  file=sys.stderr)
            return 2

        # Derive missing fields
        usd_sek = args.usd_sek
        if usd_sek is None:
            # amount_sek = btc * btc_price_local; btc_price_local = btc_usd * usd_sek
            # → usd_sek = amount_sek / (btc * btc_usd)
            usd_sek = args.amount_sek / (args.btc * args.btc_usd)
        btc_price_local = args.btc_usd * usd_sek

        rid = await repo.record_hodl_purchase(
            amount_local=args.amount_sek,
            btc_amount=args.btc,
            btc_price_usd=args.btc_usd,
            local_currency="SEK",
            btc_price_local=btc_price_local,
            fx_rate=usd_sek,
            zone=args.zone,
            exchange=args.exchange,
            notes=args.notes,
        )
        print(
            f"Recorded purchase #{rid}: {args.btc} BTC @ ${args.btc_usd:,.0f} "
            f"= {args.amount_sek:,.0f} SEK (anskaffningsvärde {btc_price_local:,.0f} SEK/BTC, "
            f"USD/SEK={usd_sek:.3f}, zone={args.zone or 'n/a'})"
        )
        return 0
    finally:
        await repo.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
