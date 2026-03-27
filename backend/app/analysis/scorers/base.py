"""Base scorer interface for the multi-class detection system.

Every attack class implements a scorer that inherits from BaseScorer.
The orchestrator (engine.py) calls gate() then score() for each scorer,
assembling a 9-element score vector per transaction.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ScorerResult:
    """Output of a single scorer for one transaction."""

    score: float = 0.0
    """Risk score in 0-100 range."""

    sub_scores: Dict[str, float] = field(default_factory=dict)
    """Individual sub-score breakdown (feature_name -> normalised value)."""

    reasons: List[str] = field(default_factory=list)
    """Human-readable contributing factors."""

    baseline_source: str = "missing"
    """Which baseline tier was used: per_script, per_policy, global, fixed, missing."""

    severity: Optional[str] = None
    """Scorer-specific severity classification (e.g. KNOWN_BAD, SUSPICIOUS_NEW_DOMAIN)."""


class BaseScorer(ABC):
    """Abstract base for all attack-class scorers."""

    name: str = ""
    """Machine-readable attack class name (e.g. 'phishing')."""

    @abstractmethod
    def gate(self, features: Dict[str, Any]) -> bool:
        """Return True if this transaction/UTxO should be scored by this class.

        Gate conditions are hard prerequisites (e.g. 'must be a script address').
        If gate() returns False, the scorer is skipped and the class score is -1.
        """

    @abstractmethod
    def score(self, features: Dict[str, Any]) -> ScorerResult:
        """Compute the weighted composite risk score.

        Called only when gate() returns True.  The features dict contains all
        extracted and enriched fields for the transaction, including baselines
        resolved by the orchestrator.
        """
