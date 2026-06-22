"""Projection of the clustering sidecar's verdict onto the host score scale.

The clustering sidecar classifies each watched-contract transaction relative to
its contract's population and produces a verdict (``malicious`` / ``benign`` /
``anomaly`` / ``normal``) plus an ensemble ``consensus`` in ``[0, 1]``. This
module maps that onto the host's 0-100 score and :class:`RiskBand` so the
finding can surface as the synthetic ``contract_anomaly`` attack class.

It is a pure function of the verdict + consensus and the validated
``contract_anomaly`` config block (no magic numbers): the sidecar calls it at
write time to populate ``tx_contract_anomaly.score`` / ``risk_band``. The host
API never recomputes the score; it reads the stored value and merges it
additively.

WHY votes are not an input: the engine's verdict precedence already folds the
detector-vote count into the ``anomaly`` verdict (``votes >= FLAG_VOTE_THRESHOLD``
upstream), so votes are fully determined by the verdict at the anomaly boundary
and would double-count here. They are stored as evidence by the sidecar, not
used to drive the score.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.analysis.normalise import score_to_band
from app.analysis.scorer_config import contract_anomaly_config
from app.models.transaction import RiskBand

# Bounds of the host score scale. 0 and 100 are the documented endpoints of the
# 0-100 risk score (see RiskBand); kept named so the clamp reads as intent.
_SCORE_MIN = 0.0
_SCORE_MAX = 100.0


def project_score(verdict: str, consensus: float | None) -> tuple[float, RiskBand]:
    """Map a clustering verdict + consensus onto ``(score, RiskBand)``.

    ``score = clamp(max(verdict_floor, consensus * consensus_scale))``: the
    verdict supplies a floor (a human-labeled malicious cluster floors into
    Critical, an auto-anomaly into High) and the ensemble consensus can raise a
    no-floor verdict on its own. An unknown verdict gets no floor (treated as
    ``normal``); a missing consensus contributes nothing. The mapping only ever
    produces a score, never a side effect, so it is safe to call per transaction.
    """
    cfg = contract_anomaly_config()
    floors = cfg["verdict_floors"]
    floor = float(floors.get(verdict, floors["normal"]))
    consensus_term = float(consensus) * float(cfg["consensus_scale"]) if consensus is not None else 0.0
    score = max(floor, consensus_term)
    score = min(_SCORE_MAX, max(_SCORE_MIN, score))
    return score, RiskBand(score_to_band(score))


def resolve(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Collapse a transaction's raw verdict rows to the highest-severity one,
    with the host-scale score + band computed.

    A transaction touched by several watched contracts has one raw row per
    contract. We score each via :func:`project_score` and keep the highest, so
    a later benign verdict for one contract can never hide an anomaly verdict
    for another. Returns ``None`` for an empty input. The returned dict carries
    the computed ``score`` / ``risk_band`` plus the winning row's raw fields.
    """
    best: Optional[Dict[str, Any]] = None
    best_score = -1.0
    for row in rows:
        consensus = row.get("consensus")
        score, band = project_score(
            row.get("verdict", "normal"),
            float(consensus) if consensus is not None else None,
        )
        if score > best_score:
            best_score = score
            best = {**row, "score": score, "risk_band": band}
    return best


def corroboration_threshold() -> float:
    """The contract_anomaly score at or above which the verdict counts as a
    corroborating signal for analyst triage (see detection.yaml)."""
    return float(contract_anomaly_config()["corroboration_threshold"])


def freshness_seconds() -> int:
    """Age beyond which a merged contract_anomaly verdict is considered stale
    (the sidecar may be down). 0 disables the staleness stamp."""
    return int(contract_anomaly_config()["freshness_seconds"])
