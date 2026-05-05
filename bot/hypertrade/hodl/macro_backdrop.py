"""Macro backdrop signal — global liquidity / dollar / recession context.

Distinct from btc_accumulation_zone (which uses Bitcoin-specific on-chain
data) — this signal asks "is the broader macro environment supportive
for risk assets right now?" When green, BTC tailwinds are present even
if on-chain doesn't yet say bottom. When red, even a "perfect" on-chain
setup can fail because the sea is going out.

v1 has only DXY. Will grow with Global Liquidity and Recession when
those HARs are extracted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hypertrade.data import roots_local
from hypertrade.hodl.base import Check, Signal, SignalState
from hypertrade.hodl.registry import register

logger = logging.getLogger(__name__)


@register
class MacroBackdropSignal(Signal):
    name = "macro_backdrop"
    asset = "BTC"
    description = (
        "Macro environment for risk-on positioning. DXY weakness, ample "
        "global liquidity, and no recession all support adding to BTC. "
        "When most checks fail, defer additions even if on-chain says bottom."
    )
    threshold = 0.5  # ≥ half of present checks must pass

    # DXY thresholds
    dxy_weak_threshold: float = 100.0   # < 100 = USD relatively weak
    dxy_strong_threshold: float = 105.0 # > 105 = strong dollar headwind

    def _verdict(self, score: float) -> str:
        if score >= 0.8:
            return "Tailwind — strongly risk-on macro"
        if score >= self.threshold:
            return "Mild tailwind — broadly supportive"
        if score >= 0.3:
            return "Mixed — neutral macro"
        return "Headwind — macro fights BTC"

    async def evaluate(self) -> SignalState:
        checks: list[Check] = []
        notes_parts: list[str] = []

        # DXY check
        try:
            dxy_series = roots_local.load_dxy()
            if dxy_series:
                latest = roots_local.latest(dxy_series)
                if latest:
                    dxy_date, dxy_val = latest
                    age = (datetime.now(timezone.utc).date() - dxy_date).days
                    is_weak = dxy_val < self.dxy_weak_threshold
                    is_strong = dxy_val > self.dxy_strong_threshold
                    state_label = (
                        "weak" if is_weak else "strong" if is_strong else "neutral"
                    )
                    checks.append(Check(
                        name="DXY weak (USD risk-on)",
                        passed=is_weak,
                        value=f"DXY = {dxy_val:.2f} ({state_label}, {age}d old)",
                        threshold=f"< {self.dxy_weak_threshold:.0f}",
                    ))
                    if is_strong:
                        notes_parts.append(
                            f"⚠ DXY {dxy_val:.1f} > {self.dxy_strong_threshold:.0f} "
                            f"= active strong-dollar headwind."
                        )
        except Exception:
            logger.exception("macro_backdrop: DXY load failed")

        if not checks:
            notes_parts.append(
                "No macro data available yet. Extract HAR exports from "
                "/dxy, /global-liquidity, /recession to populate checks."
            )

        notes_parts.append(
            "v1: only DXY. Global Liquidity and Recession to follow."
        )

        # Use _build_state to get standard score calc + verdict mapping
        return self._build_state(checks, notes=" ".join(notes_parts))
