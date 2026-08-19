"""Contract filter and the contract-grouped alerts aggregate.

The filter tests are pure (the WHERE builder never touches ClickHouse); the
aggregate tests use a fake client so the emitted SQL is asserted directly. That
matters here because two of the constraints are properties of the SQL text: the
aggregate aliases must not shadow source columns (ClickHouse 26.x Code 184), and
the grouped view must exclude rows naming no contract.
"""

from app.analysis.contract_identity import NO_CONTRACT
from app.db import clickhouse_scores
from app.db.clickhouse import _score_filter_conditions


def _build(**overrides):
    kwargs = dict(
        network="preprod",
        risk_band=None,
        attack_class=None,
        min_score=0.0,
        analyzed_from=None,
        analyzed_to=None,
        include_archived=True,  # skip the anti-join for a focused check
    )
    kwargs.update(overrides)
    return _score_filter_conditions(**kwargs)


class TestContractFilterTriState:
    def test_none_means_no_filter(self):
        conditions, params = _build(contract=None)
        assert not any("contract_address" in c for c in conditions)
        assert "contract" not in params

    def test_default_is_no_filter(self):
        conditions, params = _build()
        assert not any("contract_address" in c for c in conditions)

    def test_address_restricts_to_that_contract(self):
        conditions, params = _build(contract="addr_test1wq9")
        assert "contract_address = %(contract)s" in conditions
        assert params["contract"] == "addr_test1wq9"

    def test_empty_string_selects_the_no_contract_rows(self):
        # This is what the grouped view uses to fetch its ungrouped rows, so it
        # must be a real filter rather than being read as "no filter".
        conditions, params = _build(contract=NO_CONTRACT)
        assert "contract_address = %(contract)s" in conditions
        assert params["contract"] == NO_CONTRACT

    def test_parameterized_not_interpolated(self):
        conditions, params = _build(contract="addr'; DROP TABLE x --")
        assert all("DROP TABLE" not in c for c in conditions)
        assert params["contract"] == "addr'; DROP TABLE x --"


class _FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []
        self.params: list[dict] = []

    def execute(self, query, params=None):
        self.queries.append(query)
        self.params.append(params or {})
        return self.rows


def _patch_client(monkeypatch, rows):
    fake = _FakeClient(rows)
    monkeypatch.setattr(clickhouse_scores, "_client", lambda: fake)
    return fake


class TestGroupedAggregateSql:
    def test_aliases_do_not_shadow_source_columns(self, monkeypatch):
        # ClickHouse 26.x raises Code 184 when an aggregate alias shadows a column
        # a sibling aggregate references, so max_score/risk_band/analyzed_at must
        # not be reused as alias names.
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod")
        sql = fake.queries[0]
        assert "AS worst_score" in sql
        assert "AS worst_pair" in sql
        assert "AS classes" in sql
        assert "AS latest_analyzed_at" in sql
        for shadowed in ("AS max_score", "AS risk_band", "AS analyzed_at", "AS max_class"):
            assert shadowed not in sql

    def test_excludes_rows_naming_no_contract(self, monkeypatch):
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert "contract_address != %(no_contract)s" in fake.queries[0]
        assert fake.params[0]["no_contract"] == NO_CONTRACT

    def test_groups_by_contract(self, monkeypatch):
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert "GROUP BY contract_address" in fake.queries[0]

    def test_band_and_class_come_from_ONE_argmax(self, monkeypatch):
        # The band is argMax, not re-derived from worst_score: a past
        # recalibration can move the thresholds and the badge must agree with the
        # row the analyst sees on expanding the group.
        #
        # And band and class come out of a single argMax over a TUPLE. Two
        # separate argMax calls sharing an ordering can break a tie on different
        # rows, which would put a severity badge and an attack type belonging to
        # two different alerts on one line.
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert "argMax((risk_band, max_class), max_score) AS worst_pair" in fake.queries[0]
        assert "argMax(risk_band, max_score)" not in fake.queries[0]
        assert "argMax(max_class, max_score)" not in fake.queries[0]
        # Aliased away from the source column names: on 26.x an aggregate aliased
        # to a column a sibling aggregate reads returns Code 184.
        assert "AS max_class" not in fake.queries[0]
        assert "AS max_score" not in fake.queries[0]

    def test_distinct_classes_are_collected_and_sorted(self, monkeypatch):
        # Sorted so an unchanged group does not reshuffle its "+N more" tooltip
        # between two identical refreshes; groupUniqArray has no stable order.
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert "arraySort(groupUniqArray(max_class)) AS classes" in fake.queries[0]

    def test_rows_are_mapped_by_name(self, monkeypatch):
        _patch_client(
            monkeypatch,
            [
                (
                    "addr_test1wq9",
                    12,
                    92.5,
                    # One argMax over a tuple, so the driver hands back the band
                    # and the class of the SAME row as one value.
                    ("Critical", "large_datum"),
                    ("large_datum", "large_value"),
                    "2026-08-01 10:00:00",
                )
            ],
        )
        groups = clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert groups == [
            {
                "contract_address": "addr_test1wq9",
                "alert_count": 12,
                "worst_score": 92.5,
                "worst_band": "Critical",
                "worst_class": "large_datum",
                # The driver hands back an array column as a tuple; the row map
                # normalises it so callers can treat it as a plain list.
                "classes": ["large_datum", "large_value"],
                "latest_analyzed_at": "2026-08-01 10:00:00",
            }
        ]

    def test_sort_orders_are_bounded_to_the_known_set(self, monkeypatch):
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod", sort="score")
        assert "ORDER BY worst_score DESC, latest_analyzed_at DESC" in fake.queries[0]
        clickhouse_scores.group_class_scores_by_contract(network="preprod", sort="date")
        assert "ORDER BY latest_analyzed_at DESC, worst_score DESC" in fake.queries[1]
        # An unknown sort must not reach the SQL; it falls back to the default.
        clickhouse_scores.group_class_scores_by_contract(network="preprod", sort="'; DROP --")
        assert "DROP" not in fake.queries[2]
        assert "ORDER BY latest_analyzed_at DESC" in fake.queries[2]

    def test_applies_the_same_filters_as_the_flat_list(self, monkeypatch):
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(
            network="preprod",
            risk_band=["High"],
            min_score=1.0,
            min_corroboration=2,
        )
        sql, params = fake.queries[0], fake.params[0]
        assert "lower(risk_band) IN" in sql
        assert "max_score >= %(min_score)s" in sql
        assert "corroboration_count >= %(min_corroboration)s" in sql
        assert params["min_score"] == 1.0
