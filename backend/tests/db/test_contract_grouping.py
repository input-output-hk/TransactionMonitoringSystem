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
        assert "AS worst_band" in sql
        assert "AS latest_analyzed_at" in sql
        for shadowed in ("AS max_score", "AS risk_band", "AS analyzed_at"):
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

    def test_worst_band_is_the_stored_band_of_the_worst_row(self, monkeypatch):
        # argMax, not a band re-derived from worst_score: a past recalibration can
        # move the thresholds and the badge must agree with the row the analyst
        # sees on expanding the group.
        fake = _patch_client(monkeypatch, [])
        clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert "argMax(risk_band, max_score)" in fake.queries[0]

    def test_rows_are_mapped_by_name(self, monkeypatch):
        _patch_client(
            monkeypatch,
            [("addr_test1wq9", 12, 92.5, "Critical", "2026-08-01 10:00:00")],
        )
        groups = clickhouse_scores.group_class_scores_by_contract(network="preprod")
        assert groups == [
            {
                "contract_address": "addr_test1wq9",
                "alert_count": 12,
                "worst_score": 92.5,
                "worst_band": "Critical",
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


class TestGroupCount:
    def test_counts_groups_not_alerts(self, monkeypatch):
        # The grouped pager's total is a group count; feeding it an alert count
        # would overstate the number of pages.
        fake = _patch_client(monkeypatch, [(7,)])
        assert clickhouse_scores.count_contract_groups(network="preprod") == 7
        assert "uniqExact(contract_address)" in fake.queries[0]

    def test_excludes_the_no_contract_rows(self, monkeypatch):
        fake = _patch_client(monkeypatch, [(0,)])
        clickhouse_scores.count_contract_groups(network="preprod")
        assert "contract_address != %(no_contract)s" in fake.queries[0]

    def test_empty_result_is_zero(self, monkeypatch):
        _patch_client(monkeypatch, [])
        assert clickhouse_scores.count_contract_groups(network="preprod") == 0
