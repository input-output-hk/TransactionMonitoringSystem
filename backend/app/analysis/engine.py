"""Mock Analysis Engine — Milestone 1

Reads unanalyzed transactions from ClickHouse, applies deterministic scoring
rules, and writes results back to ClickHouse in the tx_analysis_results table.

Scoring is intentionally rule-based and reproducible (no ML model) so that
the M1 integration test can be verified without training data.  A real
Analysis Engine would replace _score_transaction() with its own model.

Risk signals used:
  - Fee > 2 ADA                    → +0.20  (complex script execution)
  - input_count  > 5               → +0.15  (possible UTXO fan-in / mixing)
  - output_count > 10              → +0.15  (possible payment distribution)
  - total_output_value > 100k ADA  → +0.30  (whale transaction)
  - metadata present               → +0.10  (DeFi / protocol interaction marker)
  - unique_input_addresses > 10    → +0.15  (fan-in from many distinct sources — mixing/aggregation)
  - max_address_tx_count   > 100   → +0.10  (high-activity address involved — exchange/mixer/DEX)
  - resolved_input_value   > 100k ADA → +0.20  (whale by resolved input value, if resolvable)

Risk levels:  LOW < 0.3  ·  MEDIUM 0.3–0.6  ·  HIGH > 0.6

Cluster label: deterministic SHA-256 of the first output address, mod 100.
Anomaly flag:  risk_score >= 0.6  OR  fee == 0.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db import clickhouse

logger = logging.getLogger(__name__)

# Lovelace thresholds
_HIGH_FEE_LOVELACE      = 2_000_000          # 2 ADA
_WHALE_VALUE_LOVELACE   = 100_000_000_000    # 100,000 ADA

# Enrichment thresholds
_HIGH_FAN_IN_SOURCES    = 10   # unique source addresses funding one tx
_HIGH_ACTIVITY_TX_COUNT = 100  # total txs an address has been involved in


def _has_danger_message(metadata_str: str) -> bool:
    """Return True if the tx carries a CIP-20 msg containing 'Danger' (showcase flag)."""
    if not metadata_str or metadata_str in ("", "{}"):
        return False
    try:
        meta = json.loads(metadata_str)
        msg = meta.get("674", {}).get("msg", [])
        return any("Danger" in str(m) for m in (msg if isinstance(msg, list) else [msg]))
    except Exception:
        return False


def _score_transaction(
    row: dict[str, Any],
    resolved_input_value: int = 0,
    unique_input_addresses: int = 0,
    max_address_tx_count: int = 0,
) -> dict[str, Any]:
    """Return a fully populated analysis result dict for a single transaction row."""
    tx_hash: str = row["tx_hash"]
    fee: int = row["fee"]
    input_count: int = row["input_count"]
    output_count: int = row["output_count"]
    total_output_value: int = row["total_output_value"]
    has_metadata: bool = bool(row.get("metadata") and row["metadata"] not in ("", "{}"))
    addresses: list[str] = row.get("addresses") or []

    # Showcase: transactions explicitly flagged with msg:"Danger" → instant max risk
    if _has_danger_message(row.get("metadata", "")):
        seed = addresses[0] if addresses else tx_hash
        cluster_id = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 100
        return {
            "tx_hash": tx_hash,
            "network": row["network"],
            "risk_score": 1.0,
            "risk_level": "HIGH",
            "cluster_id": cluster_id,
            "is_anomaly": 1,
            "anomaly_reasons": ["msg_danger_flag"],
            "analysis_version": settings.ANALYSIS_ENGINE_VERSION,
            "analyzed_at": datetime.now(timezone.utc),
        }

    risk = 0.0
    reasons: list[str] = []

    if fee > _HIGH_FEE_LOVELACE:
        risk += 0.20
        reasons.append(f"high_fee:{fee}")

    if input_count > 5:
        risk += 0.15
        reasons.append(f"high_input_count:{input_count}")

    if output_count > 10:
        risk += 0.15
        reasons.append(f"high_output_count:{output_count}")

    if total_output_value > _WHALE_VALUE_LOVELACE:
        risk += 0.30
        reasons.append(f"high_value:{total_output_value}")

    if has_metadata:
        risk += 0.10
        reasons.append("has_metadata")

    if fee == 0:
        reasons.append("zero_fee")

    # Enrichment signals — UTxO lineage, input resolution, address activity
    if unique_input_addresses > _HIGH_FAN_IN_SOURCES:
        risk += 0.15
        reasons.append(f"high_fan_in_unique_sources:{unique_input_addresses}")

    if max_address_tx_count > _HIGH_ACTIVITY_TX_COUNT:
        risk += 0.10
        reasons.append(f"high_activity_address_tx_count:{max_address_tx_count}")

    if resolved_input_value > _WHALE_VALUE_LOVELACE:
        risk += 0.20
        reasons.append(f"high_resolved_input_value:{resolved_input_value}")

    risk_score = round(min(1.0, max(0.0, risk)), 4)

    if risk_score >= 0.6:
        risk_level = "HIGH"
    elif risk_score >= 0.3:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    # Deterministic address cluster: SHA-256 of first address, mod 100
    seed = addresses[0] if addresses else tx_hash
    cluster_id = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % 100

    is_anomaly = risk_score >= 0.6 or fee == 0

    return {
        "tx_hash": tx_hash,
        "network": row["network"],
        "risk_score": risk_score,
        "risk_level": risk_level,
        "cluster_id": cluster_id,
        "is_anomaly": 1 if is_anomaly else 0,
        "anomaly_reasons": reasons,
        "analysis_version": settings.ANALYSIS_ENGINE_VERSION,
        "analyzed_at": datetime.now(timezone.utc),
    }


def run_once(network: str) -> int:
    """Score one batch of unanalyzed transactions.  Returns the count scored."""
    rows = clickhouse.get_unanalyzed_transactions(network, settings.ANALYSIS_ENGINE_BATCH_SIZE)
    if not rows:
        return 0

    tx_hashes = [r["tx_hash"] for r in rows]
    all_addresses = list({addr for r in rows for addr in (r.get("addresses") or [])})

    # Enrichment queries — UTxO lineage resolution and address activity
    input_resolution = clickhouse.get_input_resolution(tx_hashes, network)
    address_activity = clickhouse.get_address_activity(all_addresses, network)

    results = []
    for row in rows:
        resolution = input_resolution.get(row["tx_hash"], {})
        addr_counts = [address_activity.get(a, 0) for a in (row.get("addresses") or [])]
        results.append(_score_transaction(
            row,
            resolved_input_value=resolution.get("resolved_input_value", 0),
            unique_input_addresses=resolution.get("unique_input_addresses", 0),
            max_address_tx_count=max(addr_counts) if addr_counts else 0,
        ))

    clickhouse.insert_analysis_results(results)

    high = sum(1 for r in results if r["risk_level"] == "HIGH")
    anomalies = sum(1 for r in results if r["is_anomaly"])
    logger.info(
        f"Analysis Engine [{network}]: scored {len(results)} txs "
        f"(HIGH={high}, anomalies={anomalies})"
    )
    return len(results)


async def run_once_async(network: str) -> int:
    """Non-blocking wrapper: runs run_once on the dedicated ClickHouse executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(clickhouse._ch_executor, run_once, network)
