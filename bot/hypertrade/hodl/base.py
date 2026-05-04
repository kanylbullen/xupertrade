"""HODL signal base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Check:
    """One sub-condition of a multi-factor signal."""
    name: str
    passed: bool
    value: str  # human-readable current value
    threshold: str  # human-readable bar to clear
    weight: float = 1.0  # in case some checks count more than others later


@dataclass
class SignalState:
    """Snapshot of a signal at the moment of evaluation."""
    name: str
    asset: str
    description: str
    triggered: bool        # the headline: "is this signal firing right now?"
    score: float           # 0.0 to 1.0 — fraction of weighted checks passing
    threshold: float       # what score must be ≥ to consider it triggered
    verdict: str           # one-line summary like "Wait", "Watch", "Accumulate"
    checks: list[Check] = field(default_factory=list)
    notes: str = ""        # extra context (e.g. "buyback data not available")
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class Signal(ABC):
    """Base class for HODL signals."""

    name: str = "unnamed"
    asset: str = "BTC"
    description: str = ""
    threshold: float = 0.6  # score needed to trigger

    @abstractmethod
    async def evaluate(self) -> SignalState:
        """Return current signal state. Should NEVER raise — wrap in try/except
        and return a SignalState with error set if anything fails."""
        ...

    def _verdict(self, score: float) -> str:
        """Default verdict mapping. Override for custom phrasing."""
        if score >= 0.8:
            return "Accumulate — strong signal"
        if score >= self.threshold:
            return "Watch — conditions favorable"
        if score >= 0.4:
            return "Hold — wait for better setup"
        return "Wait — conditions weak"

    def _build_state(
        self, checks: list[Check], notes: str = "", error: str | None = None,
    ) -> SignalState:
        if not checks or error is not None:
            return SignalState(
                name=self.name, asset=self.asset, description=self.description,
                triggered=False, score=0.0, threshold=self.threshold,
                verdict="Unknown — evaluation failed" if error else "Unknown — no data",
                checks=checks or [], notes=notes, error=error,
            )
        total_weight = sum(c.weight for c in checks)
        passed_weight = sum(c.weight for c in checks if c.passed)
        score = passed_weight / total_weight if total_weight > 0 else 0.0
        return SignalState(
            name=self.name, asset=self.asset, description=self.description,
            triggered=score >= self.threshold,
            score=score, threshold=self.threshold,
            verdict=self._verdict(score), checks=checks, notes=notes,
        )
