"""Mainnet strategy allowlist (audit C3).

Extracted from `main.py` so it can be unit-tested directly. The decision
is intentionally simple: paper/testnet run the full registered set;
mainnet honors `MAINNET_ENABLED_STRATEGIES` as a fail-closed allowlist
(empty = zero strategies trade).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("hypertrade")


def apply_mainnet_allowlist(
    all_names: list[str],
    is_mainnet: bool,
    raw_csv: str,
) -> list[str]:
    """Return the subset of `all_names` that should actually trade.

    On paper/testnet the full set is returned unchanged. On mainnet the
    list is filtered to whatever appears in `raw_csv` (a comma-separated
    list, typically from `MAINNET_ENABLED_STRATEGIES`). Empty CSV =
    empty list — the bot still boots but no strategy trades. Unknown
    names in the CSV are logged and ignored. Output preserves
    registration order, not CSV order, so two boots from the same
    config produce identical lists.
    """
    if not is_mainnet:
        return list(all_names)
    raw = (raw_csv or "").strip()
    if not raw:
        logger.critical(
            "MAINNET starting with EMPTY MAINNET_ENABLED_STRATEGIES — "
            "no strategies will trade. Set MAINNET_ENABLED_STRATEGIES="
            "name1,name2 in .env and restart."
        )
        return []
    requested = {n.strip() for n in raw.split(",") if n.strip()}
    unknown = requested - set(all_names)
    if unknown:
        logger.warning(
            "MAINNET_ENABLED_STRATEGIES references unknown names "
            "(ignored): %s",
            sorted(unknown),
        )
    return [n for n in all_names if n in requested]
