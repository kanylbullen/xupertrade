"""Mainnet strategy allowlist (audit C3).

Extracted from `main.py` so it can be unit-tested directly. The decision
is intentionally simple: paper/testnet run the full registered set;
mainnet honors `MAINNET_ENABLED_STRATEGIES` as a fail-closed allowlist
(empty = zero strategies trade).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("hypertrade")

_StrategyT = TypeVar("_StrategyT")


def filter_strategies_for_tick(
    strategies: Iterable[_StrategyT],
    disabled: set[str],
    mainnet_enabled: set[str] | None,
) -> list[_StrategyT]:
    """Return the subset of `strategies` that should run this tick.

    Each strategy must have a `.name` attribute. The filter applies two
    layers:

    - `disabled`: strategy names the operator has paused via Redis
      (`hypertrade:control:disabled_strategies`).
    - `mainnet_enabled`: when not None, the per-tenant opt-in allowlist
      (UI-driven layer 2 on mainnet). A strategy must be present here
      to run; an empty set means no strategies run. Pass None on
      paper/testnet to skip this layer entirely.

    A strategy runs iff it is NOT in `disabled` AND (`mainnet_enabled`
    is None OR its name is in `mainnet_enabled`). Input order is
    preserved.
    """
    return [
        s for s in strategies
        if s.name not in disabled  # type: ignore[attr-defined]
        and (
            mainnet_enabled is None
            or s.name in mainnet_enabled  # type: ignore[attr-defined]
        )
    ]


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


def apply_tenant_allowlist(
    names: list[str],
    tenant_allowlist: list[str] | None,
) -> list[str]:
    """Filter `names` against the operator-set per-tenant allowlist
    (alembic 0016). NULL allowlist (None) = no filter (legacy
    behavior); empty list = no strategies allowed; otherwise return
    the intersection preserving input order.
    """
    if tenant_allowlist is None:
        return list(names)
    allow = set(tenant_allowlist)
    return [n for n in names if n in allow]


def parse_tenant_allowlist(raw: str | None) -> list[str] | None:
    """Parse the TENANT_ALLOWED_STRATEGIES env value.

    The dashboard orchestrator injects the tenant's operator-set
    allowlist as a JSON array at container-spawn time. It is NOT read
    from Postgres: the bot's per-tenant PG role has no grant on
    `tenants` (a dashboard-owned table), so the old
    `SELECT allowed_strategies FROM tenants` raised
    InsufficientPrivilegeError on every boot and was swallowed by a
    fail-open `except` — the allowlist silently never applied.

    Returns:
        None  — no allowlist configured (unset/blank env, matching the
                NULL semantics of `tenants.allowed_strategies`). Caller
                applies no filtering.
        list  — the allowlist. May be empty, which legitimately means
                "zero strategies may trade".

    A malformed value returns an EMPTY list, not None. This is a
    containment control, so a corrupt value must fail CLOSED rather
    than hand the tenant the unfiltered strategy set.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, list) or not all(
            isinstance(n, str) for n in parsed
        ):
            raise ValueError(
                "expected a JSON array of strings, got "
                f"{type(parsed).__name__}"
            )
    except (ValueError, TypeError):
        logger.exception(
            "TENANT_ALLOWED_STRATEGIES is malformed — failing CLOSED "
            "(no strategies will trade). Fix the value and restart."
        )
        return []
    return parsed
