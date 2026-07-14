"""Base scorer interface for the multi-class detection system.

Every attack class implements a scorer that inherits from BaseScorer.
The orchestrator (engine.py) calls gate() then score() for each scorer,
assembling a 9-element score vector per transaction.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScorerResult:
    """Output of a single scorer for one transaction."""

    score: float = 0.0
    """Risk score in 0-100 range."""

    sub_scores: dict[str, float] = field(default_factory=dict)
    """Individual sub-score breakdown (feature_name -> normalised value)."""

    reasons: list[str] = field(default_factory=list)
    """Human-readable contributing factors."""

    baseline_source: str = "missing"
    """Which baseline tier was used: per_script, per_policy, global, fixed, missing."""

    severity: str | None = None
    """Scorer-specific severity classification (e.g. KNOWN_BAD, SUSPICIOUS_NEW_DOMAIN)."""

    evidence: dict[str, Any] = field(default_factory=dict)
    """Raw evidence values for UI drill-down (addresses, byte counts, lists).

    Distinct from ``sub_scores`` (which are normalised [0,1] dimensions
    powering the donut charts): ``evidence`` carries unnormalised facts about
    the transaction that the operator should see in plain form, e.g. the
    target script address, datum byte count, or list of hop addresses.
    """

    @classmethod
    def no_finding(
        cls,
        sub_scores: dict[str, Any] | None = None,
        baseline_source: str = "missing",
        evidence: dict[str, Any] | None = None,
    ) -> "ScorerResult":
        """A gated-but-no-finding result (score -1).

        The engine treats a class score of -1 as "not applicable / no finding"
        and filters it out of ``max_class`` selection. Some scorers gate True
        (structurally engaged) but then determine the transaction is not an
        instance of the attack, e.g. a sandwich with no realized profit, a
        structural-only circular cycle, or an aggregate-only datum that no single
        output makes an alert. They return this rather than a 0+ score so the
        class surfaces in no band. ``sub_scores`` are retained for drill-down;
        ``reasons`` are necessarily empty. Centralises the -1 sentinel
        convention so it is defined in one place.
        """
        return cls(
            score=-1.0,
            sub_scores=sub_scores or {},
            reasons=[],
            baseline_source=baseline_source,
            evidence=evidence or {},
        )


# rapidfuzz library contract: ``fuzz.ratio`` returns a percentage in [0, 100].
# Scorers divide by this to bring similarity onto the [0, 1] scale the
# normalisation layer and weights expect.
FUZZ_RATIO_SCALE = 100.0


def reduce_to_best(results: Iterable[ScorerResult]) -> ScorerResult:
    """Max-reduction across per-candidate results (typically per-UTxO).

    The transaction-level verdict is the single highest-scoring candidate:
    per-UTxO scorers score every qualifying output, and recall-first means
    the worst output wins; a benign sibling output can never dilute it.
    Strictly-greater comparison keeps the first of equal-scoring candidates,
    matching the loops this replaces. When nothing qualified (or nothing
    scored above zero) the empty default (score 0, baseline "missing") is
    returned unchanged.
    """
    best = ScorerResult()
    for result in results:
        if result.score > best.score:
            best = result
    return best


def finalise_score(raw: float, scale: int = 100, ndigits: int = 2) -> float:
    """Canonical final-score contract: clip to [0, 1], scale, and round.

    Scorers accumulate a weighted sum in ``[0, 1]`` (sometimes slightly
    outside due to float noise or gate-specific boosts) and need to emit a
    value in ``[0, 100]`` rounded to two decimals. This helper centralises
    that convention so the math lives in one place.
    """
    return round(max(0.0, min(1.0, raw)) * scale, ndigits)


class BaseScorer(ABC):
    """Abstract base for all attack-class scorers."""

    name: str = ""
    """Machine-readable attack class name (e.g. 'phishing')."""

    @abstractmethod
    def gate(self, features: dict[str, Any]) -> bool:
        """Return True if this transaction/UTxO should be scored by this class.

        Gate conditions are hard prerequisites (e.g. 'must be a script address').
        If gate() returns False, the scorer is skipped and the class score is -1.
        """

    @abstractmethod
    def score(self, features: dict[str, Any]) -> ScorerResult:
        """Compute the weighted composite risk score.

        Called only when gate() returns True.  The features dict contains all
        extracted and enriched fields for the transaction, including baselines
        resolved by the orchestrator.
        """
