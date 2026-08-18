"""The Avg Risk KPI averages findings, not every scored transaction.

The alerts client filters the list to max_score >= FINDING_MIN_SCORE, so the
average shown beside that table has to apply the same floor server-side. Averaging the whole table mixed in the
mass of scored-but-clean rows (max_score = 0) that are never listed, which
dragged the KPI far below the alerts it sat next to.
"""

from app.db import clickhouse_scores
from app.models.transaction import FINDING_MIN_SCORE


class _FakeClient:
    def __init__(self, row):
        self.row = row
        self.queries: list[str] = []
        self.params: list[dict] = []

    def execute(self, query, params=None):
        self.queries.append(query)
        self.params.append(params or {})
        return [self.row]


def _stats_row(total=100, avg=61.5, finding_count=8):
    """One row shaped like the stats SELECT projection."""
    per_class = [0, 0.0, 0.0] * len(clickhouse_scores._CLASS_COLS)
    return tuple([total, 1, 2, 3, 4, avg, finding_count, None, *per_class])


def _run(monkeypatch, row=None):
    fake = _FakeClient(row or _stats_row())
    monkeypatch.setattr(clickhouse_scores, "_client", lambda: fake)
    monkeypatch.setattr(clickhouse_scores, "get_pending_count", lambda n: 0)
    clickhouse_scores._stats_cache.clear()
    stats = clickhouse_scores.get_class_scores_stats("preprod")
    return fake, stats


class TestFindingFloorInSql:
    def test_average_is_conditional_on_the_finding_floor(self, monkeypatch):
        fake, _ = _run(monkeypatch)
        sql = fake.queries[0]
        assert "avgIf(max_score, max_score >= %(finding_min_score)s) AS avg_max_score" in sql
        assert "avg(max_score) AS avg_max_score" not in sql

    def test_floor_travels_as_a_bound_parameter(self, monkeypatch):
        fake, _ = _run(monkeypatch)
        assert fake.params[0]["finding_min_score"] == FINDING_MIN_SCORE

    def test_finding_population_is_exposed(self, monkeypatch):
        # Any consumer adjusting the mean needs its real denominator; `total`
        # counts clean rows too and would skew the result.
        fake, stats = _run(monkeypatch)
        assert "countIf(max_score >= %(finding_min_score)s) AS finding_count" in fake.queries[0]
        assert stats["finding_count"] == 8

    def test_total_still_counts_every_scored_row(self, monkeypatch):
        _, stats = _run(monkeypatch)
        assert stats["total"] == 100

    def test_archive_anti_join_is_retained(self, monkeypatch):
        # The floor must not displace the archived-FP exclusion.
        fake, _ = _run(monkeypatch)
        assert "archived_alerts" in fake.queries[0]


class TestFloorValue:
    def test_floor_matches_the_alerts_client_filter(self):
        # The frontend list sends min_score=1; if these diverge the KPI and the
        # table describe different populations again.
        assert FINDING_MIN_SCORE == 1.0

    def test_floor_excludes_only_the_clean_rows(self):
        # A score of exactly 0 means every scorer gated out or found nothing.
        assert 0.0 < FINDING_MIN_SCORE
