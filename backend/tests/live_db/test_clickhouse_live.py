"""Live-ClickHouse smoke tests: real schema plus representative queries.

This is the tier that would have caught both known ClickHouse 26.x
regressions: the projection-gate DDL failure fires in execute_schema, and
aggregate-alias shadowing (Code 184) fires when the percentile/stats SQL
actually parses on the server. Rows are written under the LIVE_NETWORK
namespace with UUID hashes. Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid
from datetime import UTC, datetime

from app.db import clickhouse_scores as scores
from app.ingestion.ogmios_parser import parse_ogmios_transaction

from .conftest import LIVE_NETWORK

# tx_class_scores schema convention: -1 marks "scorer produced no finding",
# so any inserted score must be >= 0 to read back as a real signal.
_TEST_SCORE = 82.0


def _naive_utc_now() -> datetime:
    # The ClickHouse driver expects naive UTC datetimes for DateTime columns.
    return datetime.now(UTC).replace(tzinfo=None)


def _score_row(tx_hash: str) -> dict:
    return {
        "tx_hash": tx_hash,
        "network": LIVE_NETWORK,
        "token_dust": _TEST_SCORE,
        "max_score": _TEST_SCORE,
        "max_class": "token_dust",
        "risk_band": "Critical",
        "sub_scores": {"token_dust": {"asset_count": 5.0}},
        "evidence": {"token_dust": {"reasons": ["live-db smoke"]}},
        "corroboration_count": 1,
        "corroborating_classes": "token_dust",
        "analysis_version": "live-db-test",
        "analyzed_at": _naive_utc_now(),
    }


class TestSchema:
    def test_schema_reapplies_idempotently(self, ch):
        # The fixture applied it once; the app reruns it on every boot.
        # This second pass covers the CREATE-vs-existing branches,
        # including the projection migration that broke on 26.x.
        ch.execute_schema()


class TestTransactionsRoundtrip:
    def test_parsed_tx_inserts_and_reads_back(self, ch):
        tx_hash = uuid.uuid4().hex * 2
        payload = {
            "id": tx_hash,
            "spends": "inputs",
            "fee": {"ada": {"lovelace": 200_000}},
            "inputs": [{"transaction": {"id": "11" * 32}, "index": 0}],
            "outputs": [
                {
                    "address": "addr_test1qqlivedb",
                    "value": {"ada": {"lovelace": 1_500_000}},
                }
            ],
        }
        tx = parse_ogmios_transaction(
            payload,
            block_slot=1_000_000,
            block_hash="ef" * 32,
            block_height=500_000,
            timestamp=datetime.now(UTC),
        )
        tx.network = LIVE_NETWORK

        ch.insert_transactions_batch([tx])

        rows = ch._execute_query(
            """
            SELECT tx_hash, fee, total_output_value
            FROM transactions FINAL
            WHERE network = %(network)s AND tx_hash = %(tx_hash)s
            """,
            {"network": LIVE_NETWORK, "tx_hash": tx_hash},
        )
        assert len(rows) == 1
        assert rows[0][1] == 200_000
        assert rows[0][2] == 1_500_000

    def test_outputs_resolvable_for_refs(self, ch):
        # The ingestion-time UTxO resolution join against
        # transaction_outputs, exercised for real.
        tx_hash = uuid.uuid4().hex * 2
        payload = {
            "id": tx_hash,
            "fee": {"ada": {"lovelace": 170_000}},
            "outputs": [
                {
                    "address": "addr_test1qqrefsource",
                    "value": {"ada": {"lovelace": 7_000_000}},
                }
            ],
        }
        tx = parse_ogmios_transaction(payload, timestamp=datetime.now(UTC))
        tx.network = LIVE_NETWORK
        ch.insert_transactions_batch([tx])

        resolved = ch.get_outputs_for_refs([(tx_hash, 0)], LIVE_NETWORK)
        assert resolved.get((tx_hash, 0)) == ("addr_test1qqrefsource", 7_000_000)


class TestScoreReadPath:
    def test_write_read_list_count_stats(self, ch):
        tx_hash = uuid.uuid4().hex * 2
        scores.insert_class_scores([_score_row(tx_hash)])

        row = scores.get_class_scores(tx_hash, LIVE_NETWORK)
        assert row is not None
        assert row["max_class"] == "token_dust"
        assert row["max_score"] == _TEST_SCORE
        assert row["sub_scores"]["token_dust"]["asset_count"] == 5.0

        listed = scores.get_class_scores_list(
            network=LIVE_NETWORK,
            risk_band=["Critical"],
            min_score=_TEST_SCORE - 1.0,
            limit=100,
        )
        assert any(r["tx_hash"] == tx_hash for r in listed)

        assert scores.count_class_scores(network=LIVE_NETWORK, risk_band=["Critical"]) >= 1

        # The stats aggregate is where an aggregate-alias rename would
        # blow up server-side; keys are consumed by the dashboard tiles.
        stats = scores.get_class_scores_stats(LIVE_NETWORK)
        assert isinstance(stats, dict) and stats

        timeseries = scores.get_alert_timeseries(LIVE_NETWORK, days=7)
        assert isinstance(timeseries, list)


class TestBaselines:
    def test_insert_then_get_roundtrip(self, ch):
        # Fresh scope_id per run so the read misses the in-process TTL
        # cache and actually goes to the server.
        scope_id = f"live-{uuid.uuid4().hex[:16]}"
        computed_at = _naive_utc_now()
        ch.insert_baselines(
            [
                (
                    LIVE_NETWORK,
                    "script",
                    scope_id,
                    "output_count",
                    2.0,
                    9.0,
                    120,
                    computed_at,
                    180,
                )
            ]
        )
        baseline = ch.get_baseline(LIVE_NETWORK, "script", scope_id, "output_count")
        assert baseline is not None
        assert baseline["p50"] == 2.0
        assert baseline["p99"] == 9.0
        assert baseline["sample_count"] == 120

        scoped = ch.get_baselines_for_scope(LIVE_NETWORK, "script", scope_id)
        assert any(b["feature"] == "output_count" for b in scoped)

    def test_percentile_recompute_sql_parses_on_server(self, ch):
        # compute_global_baselines runs the quantile SQL over
        # utxo_features and tx_script_features on the real server; an
        # alias-shadowing regression (Code 184 on 26.x) raises here even
        # with zero rows in the window.
        from app.analysis import baselines

        rows = baselines.compute_global_baselines(LIVE_NETWORK)
        assert isinstance(rows, list)
