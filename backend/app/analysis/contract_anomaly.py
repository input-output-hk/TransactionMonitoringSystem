"""Projection of the clustering sidecar's verdict onto the host score scale.

The clustering sidecar classifies each watched-contract transaction relative to
its contract's population and produces a verdict (``malicious`` / ``benign`` /
``anomaly`` / ``normal``) plus an ensemble ``consensus`` in ``[0, 1]``. This
module maps that onto the host's 0-100 score and :class:`RiskBand` so the
finding can surface as the synthetic ``contract_anomaly`` attack class.

It is a pure function of the verdict + consensus and the validated
``contract_anomaly`` config block (no magic numbers). The host recomputes the
0-100 score + band from the sidecar's RAW fields (verdict, consensus) at READ
time: the sidecar stores only those raw outputs, not a host-scale score (see
``app.db.clustering_queries``), and :func:`resolve` projects them via this
module on every read. So this projection is the single source of truth and a
change to it (or to the config floors) applies to all historical rows with no
backfill; the host never trusts a precomputed score.

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

# Verdicts that can carry a finding. A POSITIVE verdict (a human-labeled
# malicious cluster, or an auto-anomaly) is scored from its configured floor and
# refined upward by consensus. Every OTHER verdict (benign = human-labeled safe,
# normal = no finding, and any unrecognised label) is a safe / no-finding label
# that SUPPRESSES to 0 unconditionally, so the ensemble consensus can never
# manufacture a finding from a curated-safe verdict.
#
# Suppression keys on the verdict IDENTITY, not on the numeric floor: the
# `anomaly` floor is 0 (pure consensus-driven, so it reaches High only when
# consensus scales to BAND_HIGH_THRESHOLD), so a "floor <= 0 means suppress" test
# would wrongly zero out every anomaly. The distinction between a safe verdict
# and a positive verdict that happens to have a 0 floor must therefore be the
# label, not the value.
#
# This set is the AUTHORITY for which verdicts score. To add a positive verdict
# you MUST update BOTH this set AND verdict_floors in detection.yaml: a
# config-only addition falls through to the suppress branch and projects to 0 (a
# silent, recall-negative drop). The import-time guard below keeps the two in
# sync for the KeyError case (a positive verdict with no configured floor).
_POSITIVE_VERDICTS = frozenset({"malicious", "anomaly"})

# Fail loud at import if a positive verdict has no configured floor: the
# cfg["verdict_floors"][verdict] lookup in project_score would otherwise KeyError
# at request time. Mirrors multiple_sat's import-time floor guard.
_missing_positive_floors = _POSITIVE_VERDICTS.difference(
    contract_anomaly_config()["verdict_floors"]
)
if _missing_positive_floors:
    raise RuntimeError(
        "contract_anomaly positive verdicts missing a configured floor: "
        f"{sorted(_missing_positive_floors)}. Add them to verdict_floors in "
        "config/detection.yaml (or remove them from _POSITIVE_VERDICTS)."
    )


def project_score(verdict: str, consensus: float | None) -> tuple[float, RiskBand]:
    """Map a clustering verdict + consensus onto ``(score, RiskBand)``.

    Only the POSITIVE verdicts carry a score (see :data:`_POSITIVE_VERDICTS`): a
    human-labeled malicious cluster floors into Critical, while an auto-anomaly
    carries NO floor and is driven purely by the ensemble consensus, so it bands
    Informational / Moderate / High by strength and reaches High only once
    consensus scales to ``BAND_HIGH_THRESHOLD`` (consensus >= 0.60 at the default
    scale of 100). ``benign`` (human-labeled safe), ``normal`` (no finding), and
    any unknown verdict SUPPRESS the signal outright: they project to 0 regardless
    of consensus, so a curated "safe" label is authoritative and the ensemble can
    never manufacture a finding from it. For a positive verdict the consensus may
    only REFINE the score upward from its floor (``max(floor, consensus * scale)``);
    a missing consensus contributes nothing, so a malicious verdict still floors to
    Critical while an anomaly with no consensus suppresses to 0. The mapping only
    ever produces a score, never a side effect, so it is safe to call per
    transaction.
    """
    cfg = contract_anomaly_config()
    # Suppression is keyed on the verdict identity, not the floor value: a safe
    # (benign) / no-finding (normal) / unrecognised label must never be lifted
    # into a finding by consensus, so it projects to 0. A positive verdict
    # (malicious / anomaly) is scored from its configured floor below; both keys
    # are required by the config validator, so the lookup cannot KeyError.
    if verdict not in _POSITIVE_VERDICTS:
        return _SCORE_MIN, RiskBand(score_to_band(_SCORE_MIN))
    floor = float(cfg["verdict_floors"][verdict])
    consensus_term = float(consensus) * float(cfg["consensus_scale"]) if consensus is not None else 0.0
    score = min(_SCORE_MAX, max(floor, consensus_term))
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
