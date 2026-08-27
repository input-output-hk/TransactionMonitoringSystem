"""The score insert row must stay aligned with the column list.

`insert_class_scores` names its columns from `_SCORE_COLS` and then builds each
row as a hand-ordered tuple. Those two are edited separately, so they can drift:
adding a column without adding its value shifts every later value by one and
ClickHouse accepts it silently when the types happen to line up. These tests are
the guard, which is why the width assertion matters more than it looks.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.db.clickhouse_scores import _SCORE_COLS, insert_class_scores


def _result(**overrides):
    row = {
        "tx_hash": "tx01",
        "network": "preprod",
        "max_score": 42.0,
        "max_class": "large_value",
        "risk_band": "Moderate",
        "sub_scores": {},
        "evidence": {},
        "analysis_version": "phase5",
        "analyzed_at": datetime(2026, 8, 20, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def _inserted_rows(results):
    client = MagicMock()
    with patch("app.db.clickhouse_scores._client", return_value=client):
        insert_class_scores(results)
    assert client.execute.call_count == 1
    return client.execute.call_args.args[1]


class TestInsertRowAlignment:
    def test_row_width_matches_the_column_list(self):
        (row,) = _inserted_rows([_result()])
        assert len(row) == len(_SCORE_COLS)

    def test_provenance_lands_in_its_own_columns(self):
        (row,) = _inserted_rows([_result(config_hash="a" * 64, code_version="cafe123")])
        assert row[_SCORE_COLS.index("config_hash")] == "a" * 64
        assert row[_SCORE_COLS.index("code_version")] == "cafe123"

    def test_absent_provenance_inserts_blank_rather_than_raising(self):
        """An imported or hand-built row has no provenance to offer, and '' is
        the same "not recorded" that pre-migration rows carry."""
        (row,) = _inserted_rows([_result()])
        assert row[_SCORE_COLS.index("config_hash")] == ""
        assert row[_SCORE_COLS.index("code_version")] == ""

    def test_columns_after_provenance_are_not_shifted(self):
        analyzed_at = datetime(2026, 8, 20, 12, 30, tzinfo=UTC)
        (row,) = _inserted_rows([_result(analyzed_at=analyzed_at)])
        assert row[_SCORE_COLS.index("analyzed_at")] == analyzed_at

    def test_empty_input_issues_no_query(self):
        client = MagicMock()
        with patch("app.db.clickhouse_scores._client", return_value=client):
            insert_class_scores([])
        client.execute.assert_not_called()
