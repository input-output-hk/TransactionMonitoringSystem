"""Read-only access to the clustering sidecar's contract_anomaly verdicts.

The optional clustering sidecar writes one row per ``(network, tx_hash,
target)`` to ``<CLUSTERING_DB>.tx_contract_anomaly`` on the SAME ClickHouse
server as the host's ``tms_analytics``. It stores the RAW engine outputs
(verdict, consensus, votes, ...) and deliberately NOT a host-scale score: the
0-100 score + risk band are computed by the host from these raw fields via the
``contract_anomaly`` projection config, so the mapping has a single source of
truth (the host's detection.yaml) and the sidecar stays ignorant of the host
scoring scale.

This module only fetches the raw rows. Collapsing the several rows a single
transaction may have (one per watched contract that touched it) to the
highest-severity verdict, and mapping it onto the host score, is done by
``app.analysis.contract_anomaly.resolve`` so the projection logic lives in one
place.

Every read is best-effort: if the sidecar has never run (database/table absent)
or the server is briefly unreachable, the lookup returns empty and the caller
proceeds with the nine per-tx classes. The contract_anomaly class is purely
additive, so its absence can only omit a signal, never corrupt an existing one.

Client/executor access goes through the host ClickHouse facade AT CALL TIME (the
function-level imports), matching ``app.db.clickhouse_scores`` so tests can
monkeypatch ``app.db.clickhouse._get_client`` / ``_ch_executor``.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Raw verdict columns read from the sidecar's table. tx_hash drives grouping;
# the rest describe one (contract, transaction) verdict.
_FIELDS = (
    "target", "cluster_id", "iso_score", "lof_score", "consensus",
    "votes", "verdict", "model_id", "feature_set", "evidence", "scored_at",
)

# The engine's "no finding" verdict label (see the sidecar's verdict vocabulary:
# malicious / anomaly / benign / normal). Only NON-normal verdicts can ever
# project to a host score a list filter would surface, so the recall-rescue
# query (``flagged_for_network``) restricts to ``verdict != normal``.
_NORMAL_VERDICT = "normal"

# Safety bound on the recall-rescue fetch. The flagged set (non-normal verdicts
# for one network) is small relative to all traffic, but it grows with history;
# this caps the rescue scan so a filtered list view stays bounded. It is a
# pagination safety limit, not a detection threshold; truncation is logged (never
# silent) so an operator can raise it rather than lose a flagged tx unseen.
_RESCUE_FETCH_CAP = 10_000


def _client():
    """The host facade's per-thread Client, resolved late (monkeypatch-safe)."""
    from app.db import clickhouse
    return clickhouse._get_client()


async def _run(fn, *args):
    """Run ``fn`` on the host facade's ClickHouse executor, resolved late."""
    from app.db import clickhouse
    return await clickhouse._in_executor(fn, *args)


def _table() -> str:
    """Fully-qualified, sibling-database table name.

    ``CLUSTERING_DB`` is a trusted config constant (not user input), so it is
    interpolated into the identifier position the same way the engine qualifies
    its own tables; the row filters below are fully parameterized.
    """
    return f"{settings.CLUSTERING_DB}.tx_contract_anomaly"


def _select(where: str) -> str:
    cols = ", ".join(_FIELDS)
    return f"""
        SELECT toString(tx_hash) AS tx_hash, {cols}
        FROM {_table()} FINAL
        WHERE {where}
    """


def _row_to_dict(row: tuple) -> Dict[str, Any]:
    d = dict(zip(("tx_hash", *_FIELDS), row))
    if isinstance(d.get("evidence"), str):
        try:
            d["evidence"] = json.loads(d["evidence"])
        except (json.JSONDecodeError, TypeError):
            d["evidence"] = {}
    return d


def _group(rows: List[tuple]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        d = _row_to_dict(r)
        out.setdefault(d["tx_hash"], []).append(d)
    return out


def get_contract_anomaly(network: str, tx_hash: str) -> List[Dict[str, Any]]:
    """All raw contract_anomaly verdict rows for one transaction (one per
    watched contract that touched it). Empty list on a clean miss AND on any
    error reaching the sidecar's database (best-effort)."""
    try:
        rows = _client().execute(
            _select("network = %(network)s AND tx_hash = %(tx_hash)s"),
            {"network": network, "tx_hash": tx_hash},
        )
    except Exception as e:  # noqa: BLE001 - best-effort cross-db read
        logger.debug("contract_anomaly lookup failed for %s: %s", tx_hash, e)
        return []
    return [_row_to_dict(r) for r in rows]


def get_contract_anomaly_batch(
    network: str, tx_hashes: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Raw verdict rows grouped by tx_hash for a page of tx_hashes.

    Returns ``{tx_hash: [verdict_row, ...]}``; missing hashes are absent.
    Returns ``{}`` on any error reaching the sidecar's database."""
    if not tx_hashes:
        return {}
    try:
        rows = _client().execute(
            _select("network = %(network)s AND tx_hash IN %(hashes)s"),
            {"network": network, "hashes": tx_hashes},
        )
    except Exception as e:  # noqa: BLE001 - best-effort cross-db read
        logger.debug("contract_anomaly batch lookup failed: %s", e)
        return {}
    return _group(rows)


async def get_contract_anomaly_async(
    network: str, tx_hash: str,
) -> List[Dict[str, Any]]:
    return await _run(get_contract_anomaly, network, tx_hash)


async def get_contract_anomaly_batch_async(
    network: str, tx_hashes: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    return await _run(partial(get_contract_anomaly_batch, network, tx_hashes))


def flagged_for_network(
    network: str, limit: int = _RESCUE_FETCH_CAP,
) -> Dict[str, List[Dict[str, Any]]]:
    """Raw verdict rows (grouped by tx_hash) for every NON-normal sidecar
    finding on a network, newest first, capped at ``limit``.

    Powers the list endpoint's recall rescue: a transaction whose stored 9-class
    score is below an active filter but whose contract_anomaly verdict projects
    above it would otherwise be dropped by the DB filter (which sees only stored
    scores). Restricting to ``verdict != normal`` keeps this bounded to the
    findings that could matter. Returns ``{}`` on a clean miss or any error
    reaching the sidecar's database (best-effort). The caller logs when ``limit``
    is reached so a truncated rescue set is never silent."""
    try:
        rows = _client().execute(
            _select("network = %(network)s AND verdict != %(normal)s")
            # Order by reconciliation recency, not source time. A newly
            # malicious-labeled OLD transaction is re-published with a fresh
            # published_at but keeps its original (older) scored_at; ordering by
            # scored_at could push it past the cap and silently drop it from the
            # rescue / stats augmentation. published_at keeps the latest-touched
            # findings inside the cap, preserving recall on relabels.
            + " ORDER BY published_at DESC LIMIT %(limit)s",
            {"network": network, "normal": _NORMAL_VERDICT, "limit": limit},
        )
    except Exception as e:  # noqa: BLE001 - best-effort cross-db read
        logger.debug("contract_anomaly flagged lookup failed: %s", e)
        return {}
    return _group(rows)


async def flagged_for_network_async(
    network: str, limit: int = _RESCUE_FETCH_CAP,
) -> Dict[str, List[Dict[str, Any]]]:
    return await _run(partial(flagged_for_network, network, limit))


def latest_scored_at(network: str) -> Optional[Any]:
    """Most recent ``scored_at`` across the sidecar's verdicts for a network.

    Powers the host ``/health/detail`` freshness probe. Returns None if the
    table is empty or unreachable."""
    try:
        rows = _client().execute(
            f"SELECT max(scored_at) FROM {_table()} WHERE network = %(network)s",
            {"network": network},
        )
    except Exception as e:  # noqa: BLE001 - best-effort cross-db read
        logger.debug("contract_anomaly freshness probe failed: %s", e)
        return None
    return rows[0][0] if rows and rows[0][0] else None


async def latest_scored_at_async(network: str) -> Optional[Any]:
    return await _run(latest_scored_at, network)
