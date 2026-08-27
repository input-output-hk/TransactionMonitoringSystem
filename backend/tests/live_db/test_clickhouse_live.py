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


class TestScoreProvenance:
    """The provenance columns are LowCardinality and were added by ALTER, which
    is the pair of facts a mocked test cannot check: the hermetic suite asserts
    the value reaches the insert tuple, not that ClickHouse accepts the type or
    that the migration is replayable on a table that already has data."""

    def test_provenance_round_trips(self, ch):
        tx_hash = uuid.uuid4().hex * 2
        digest = "b" * 64
        scores.insert_class_scores(
            [_score_row(tx_hash) | {"config_hash": digest, "code_version": "c0ffee1"}]
        )
        row = scores.get_class_scores(tx_hash, LIVE_NETWORK)
        assert row is not None
        assert row["config_hash"] == digest
        assert row["code_version"] == "c0ffee1"

    def test_row_without_provenance_reads_blank(self, ch):
        """The pre-migration state: a row carrying no provenance must read as
        "not recorded" rather than failing the insert or the read."""
        tx_hash = uuid.uuid4().hex * 2
        scores.insert_class_scores([_score_row(tx_hash)])
        row = scores.get_class_scores(tx_hash, LIVE_NETWORK)
        assert row is not None
        assert row["config_hash"] == ""
        assert row["code_version"] == ""

    def test_columns_are_low_cardinality(self, ch):
        """Chosen deliberately: the distinct count is the number of tunings and
        builds ever deployed, so a 64-character digest per row must not be
        stored per row. A silent revert to plain String would cost real bytes on
        the highest-traffic table."""
        rows = ch._get_client().execute(
            "SELECT name, type FROM system.columns "
            "WHERE database = currentDatabase() AND table = 'tx_class_scores' "
            "  AND name IN ('config_hash', 'code_version')"
        )
        assert {name: type_ for name, type_ in rows} == {
            "config_hash": "LowCardinality(String)",
            "code_version": "LowCardinality(String)",
        }


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


class TestContractGrouping:
    """The grouped alerts aggregate, on a real server.

    Two reasons this cannot be left to the hermetic suite. The aggregate reads
    ``max_class`` from two places at once (an argMax over a tuple and a
    groupUniqArray), which is the shape that returns Code 184 on 26.x when an
    alias collides with a source column, and a mocked client only ever proves
    the query TEXT. And the band/class pairing is a tie-breaking property of
    argMax that no string assertion can establish.
    """

    @staticmethod
    def _row(contract: str, cls: str, score: float, band: str) -> dict:
        row = _score_row(uuid.uuid4().hex * 2)
        row["contract_address"] = contract
        row["max_class"] = cls
        row["max_score"] = score
        row["risk_band"] = band
        # Keep the per-class column in step with max_class, so the row is not
        # self-contradictory to anything that reads the vector.
        row.pop("token_dust", None)
        row[cls] = score
        return row

    def _group_for(self, contract: str) -> dict:
        groups = scores.group_class_scores_by_contract(
            network=LIVE_NETWORK, min_score=1.0, limit=100
        )
        found = [g for g in groups if g["contract_address"] == contract]
        assert found, f"no group for {contract} in {len(groups)} groups"
        return found[0]

    def test_group_counts_and_names_its_worst_alert(self, ch):
        contract = f"addr_livedb_{uuid.uuid4().hex[:12]}"
        scores.insert_class_scores(
            [
                self._row(contract, "token_dust", 82.0, "Critical"),
                self._row(contract, "large_value", 40.0, "Moderate"),
            ]
        )
        group = self._group_for(contract)
        assert group["alert_count"] == 2
        assert group["worst_score"] == 82.0
        assert group["worst_band"] == "Critical"
        assert group["worst_class"] == "token_dust"
        # Sorted server-side, so the UI's "+N more" list cannot reshuffle between
        # two identical refreshes.
        assert group["classes"] == ["large_value", "token_dust"]

    def test_band_and_class_describe_the_SAME_alert_on_a_tie(self, ch):
        # Equal max_score is where two independent argMax calls could each pick a
        # different winner and hand the UI a severity from one alert with the
        # attack type of another. One argMax over the tuple cannot: whichever row
        # wins, the pair it returns is that row's own.
        contract = f"addr_livedb_{uuid.uuid4().hex[:12]}"
        tie = 90.0
        scores.insert_class_scores(
            [
                self._row(contract, "phishing", tie, "High"),
                self._row(contract, "circular", tie, "Critical"),
            ]
        )
        group = self._group_for(contract)
        assert group["worst_score"] == tie
        assert (group["worst_band"], group["worst_class"]) in {
            ("High", "phishing"),
            ("Critical", "circular"),
        }


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


class TestArchiveTimeWindow:
    def test_to_bound_is_exclusive(self, ch):
        # The API's shared [from, to) half-open window convention: a row
        # archived exactly at the `to` instant must be EXCLUDED, so chained
        # windows never double-count the boundary. Runs against the real
        # server because the boundary comparison happens in ClickHouse SQL.
        from app.db import archive_queries

        tx_hash = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        boundary = _naive_utc_now().replace(microsecond=0)
        archive_queries._archive_insert(
            LIVE_NETWORK, tx_hash, "boundary probe", "live-db-test", boundary, "manual"
        )

        def _hashes(date_from, date_to):
            rows = archive_queries._archive_list(LIVE_NETWORK, date_from, date_to, 1000, 0)
            return {r["tx_hash"] for r in rows}

        # Window ending exactly at the row's archived_at: excluded.
        assert tx_hash not in _hashes(None, boundary)
        # Window starting exactly there: included (from is inclusive).
        assert tx_hash in _hashes(boundary, None)
