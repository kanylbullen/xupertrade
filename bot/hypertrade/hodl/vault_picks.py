"""HODL signal: aggregate state of the HyperLiquid vault watch-list.

Reads the latest snapshot from the vault scanner and reports how many
vaults are currently qualified, plus a few "pool quality" cross-checks
(at least one mature vault, at least one Sharpe > 2.0). Doesn't trade —
just tells the user "should I look at vaults right now?"

Lives alongside other HODL signals so it shows up on `/hodl` next to
btc_accumulation_zone, altseason, etc.
"""

from __future__ import annotations

import logging

from hypertrade.config import settings
from hypertrade.db.repo import Repository
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


@register
class VaultPicksSignal(Signal):
    name = "vault_picks"
    asset = "USD"
    description = (
        "Aggregate state of the HyperLiquid vault watch-list. Counts how "
        "many vaults pass our quality filter and surfaces pool-quality "
        "cross-checks. Use as a 'should I deposit?' heads-up — the table "
        "on /vaults shows the actual candidates."
    )
    # Even one qualified vault counts as the signal firing — score of 1/4
    # passes via the threshold. We mostly use checks for *quality of the pool*
    # rather than gating the verdict.
    threshold = 0.25

    min_qualified_for_solid: int = 4
    min_age_days_for_mature: int = 365
    min_sharpe_for_top_tier: float = 2.0

    def _verdict(self, score: float) -> str:
        # Overridden via context inside evaluate() once we know counts;
        # this is a fallback if score-only is requested.
        return super()._verdict(score)

    def _verdict_for_count(self, n: int) -> str:
        if n == 0:
            return "No qualified vaults — wait"
        if n <= 3:
            return f"{n} candidate{'s' if n != 1 else ''} — verify before depositing"
        if n <= 10:
            return f"{n} solid pool — pick top 2-3 by Sharpe"
        return f"{n} crowded — be selective; capacity-decay risk"

    async def evaluate(self) -> SignalState:
        try:
            repo = Repository()
        except Exception as exc:
            return self._build_state(
                [], error=f"Repository unavailable: {exc}"
            )

        try:
            qualified = await repo.latest_qualified_vaults()
        except Exception as exc:
            logger.exception("vault_picks: latest_qualified_vaults failed")
            await repo.close()
            return self._build_state([], error=str(exc))
        finally:
            try:
                await repo.close()
            except Exception:
                pass

        n = len(qualified)
        checks: list[Check] = [
            Check(
                name="At least one qualified vault",
                passed=n >= 1,
                value=f"{n} qualified",
                threshold=">= 1",
            ),
            Check(
                name="Solid candidate pool",
                passed=n >= self.min_qualified_for_solid,
                value=f"{n} qualified",
                threshold=f">= {self.min_qualified_for_solid}",
            ),
        ]

        # Quality cross-checks — only meaningful when at least one vault qualifies.
        if qualified:
            ages = [s.age_days or 0 for _, s in qualified]
            sharpes = [s.sharpe_180d or 0.0 for _, s in qualified]
            mature = sum(1 for a in ages if a >= self.min_age_days_for_mature)
            top_tier = sum(1 for sh in sharpes if sh >= self.min_sharpe_for_top_tier)

            checks.append(Check(
                name="At least one mature vault",
                passed=mature >= 1,
                value=f"{mature} >= {self.min_age_days_for_mature}d",
                threshold=">= 1",
            ))
            checks.append(Check(
                name="At least one top-tier Sharpe",
                passed=top_tier >= 1,
                value=f"{top_tier} with Sharpe(180d) >= {self.min_sharpe_for_top_tier:.1f}",
                threshold=">= 1",
            ))

        # Override the verdict to use our count-aware version.
        notes = ""
        if settings.exchange_mode != "mainnet":
            notes = (
                f"Vaults are mainnet-only on HL; the scanner runs the same "
                f"in {settings.exchange_mode} mode but can't be deposited "
                f"into from this account."
            )

        state = self._build_state(checks, notes=notes)
        # Replace the verdict with the count-aware one.
        return SignalState(
            name=state.name,
            asset=state.asset,
            description=state.description,
            triggered=state.triggered,
            score=state.score,
            threshold=state.threshold,
            verdict=self._verdict_for_count(n),
            checks=state.checks,
            notes=state.notes,
        )
