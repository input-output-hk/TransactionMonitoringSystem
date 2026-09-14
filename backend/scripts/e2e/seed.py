"""Deterministic findings for the Playwright E2E tier (scripts/e2e.sh).

Seeds a small, fixed set of scored transactions so the dashboard's default
view (High + Critical) has rows to review, archive, restore and export. Every
value is a pure function of the row index, and the score table is a
ReplacingMergeTree keyed on (network, tx_hash), so reruns dedup instead of
accumulating.

Run inside the app container, where the ClickHouse env points at the stack:

    python -m scripts.e2e.seed
"""

import hashlib
import sys
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.db import clickhouse, clickhouse_scores

# Mirrors app.db.clickhouse.insert_transactions_batch's column order (the same
# statement the perf seeder uses), so seeded rows are shaped like ingester
# output.
_TX_INSERT = """
    INSERT INTO transactions (
        tx_hash, network, slot, block_height, block_hash, block_index, timestamp, fee, deposit,
        input_count, output_count, total_input_value, total_output_value,
        addresses, metadata, raw_data, raw_data_truncated, script_valid, ingestion_timestamp
    ) VALUES
"""

# One alert per row: (class, band, score). The first six sit in the dashboard's
# default High + Critical view; the last two exist so widening the severity
# filter changes the result set (and so exports contain more than one band).
_FINDINGS = [
    ("large_datum", "Critical", 90.0),
    ("large_datum", "Critical", 86.0),
    ("phishing", "Critical", 82.0),
    ("multiple_sat", "High", 70.0),
    ("phishing", "High", 65.0),
    ("token_dust", "High", 62.0),
    ("sandwich", "Moderate", 45.0),
    ("circular", "Moderate", 35.0),
]

_ANALYSIS_VERSION = "e2e-seed-1"
# Naive-UTC epoch: the ClickHouse driver expects naive UTC datetimes.
_UNIX_EPOCH = datetime(1970, 1, 1)
# A stable, obviously synthetic ada address for the seeded rows.
_ADDR = "addr_test1e2e" + "0" * 40
_FEE_LOVELACE = 200_000
_OUTPUT_LOVELACE = 5_000_000


def tx_hash_for(i: int) -> str:
    """The seeded row's 64-hex transaction hash, derivable by the specs."""
    return hashlib.sha256(f"tms-e2e-{i}".encode()).hexdigest()


def main() -> int:
    network = settings.CARDANO_NETWORK
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    clickhouse.init_client()
    clickhouse.execute_schema()
    try:
        tx_rows, score_rows = [], []
        for i, (attack_class, band, score) in enumerate(_FINDINGS):
            tx_hash = tx_hash_for(i)
            # Spread the rows a few minutes apart, newest first on the list.
            at = now - timedelta(minutes=5 * i)
            tx_rows.append(
                (
                    tx_hash,
                    network,
                    int((at - _UNIX_EPOCH).total_seconds()),
                    1_000_000 + i,
                    hashlib.sha256(f"tms-e2e-block-{i}".encode()).hexdigest(),
                    0,
                    at,
                    _FEE_LOVELACE,
                    None,  # deposit
                    1,  # input_count
                    2,  # output_count
                    2 * _OUTPUT_LOVELACE + _FEE_LOVELACE,
                    2 * _OUTPUT_LOVELACE,
                    [_ADDR],
                    "",  # metadata
                    "",  # raw_data
                    0,  # raw_data_truncated
                    1,  # script_valid
                    at,
                )
            )
            score_rows.append(
                {
                    "tx_hash": tx_hash,
                    "network": network,
                    attack_class: score,
                    "max_score": score,
                    "max_class": attack_class,
                    "risk_band": band,
                    "sub_scores": {attack_class: {"primary_signal": 0.9, "secondary_signal": 0.6}},
                    "evidence": {attack_class: {"seed": "e2e", "index": i}},
                    "analysis_version": _ANALYSIS_VERSION,
                    "analyzed_at": at,
                }
            )
        client = clickhouse._get_client()
        client.execute(_TX_INSERT, tx_rows)
        clickhouse_scores.insert_class_scores(score_rows)
        # The network is deliberately not echoed: every attribute read off the
        # settings object is treated as sensitive by the secret scanner (that
        # object also holds the SMTP password and the signing keys), and the
        # tier's network is already stated in .env.e2e.
        print(f"seeded {len(score_rows)} findings (analysis {_ANALYSIS_VERSION})")
        for i, (attack_class, band, score) in enumerate(_FINDINGS):
            print(f"  {tx_hash_for(i)}  {attack_class:<12} {band:<8} {score}")
        return 0
    finally:
        clickhouse.close_client()


if __name__ == "__main__":
    sys.exit(main())
