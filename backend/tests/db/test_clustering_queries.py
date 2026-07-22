"""Unit tests for the clustering heartbeat query (clustering_queries).

The higher-level verdict reads are exercised by the projection / poller / window
suites; this pins the ``latest_activity_at`` heartbeat that drives the host
health dot, including the ``status = 'done'`` filter (so a feed that ticks but
fails every job cannot keep the dot green) and the best-effort failure contract.
"""


def _patch_client(monkeypatch, client):
    from app.db import clickhouse

    monkeypatch.setattr(clickhouse, "_get_client", lambda: client)


def test_latest_activity_at_reads_done_job_heartbeat(monkeypatch):
    from app.db import clustering_queries

    captured = {}

    class _FakeClient:
        def execute(self, sql, *args):
            captured["sql"] = sql
            return [("2026-07-22 10:00:00",)]

    _patch_client(monkeypatch, _FakeClient())
    out = clustering_queries.latest_activity_at()

    assert out == "2026-07-22 10:00:00"
    # Only successful completions count, so a failing/stuck feed goes stale.
    assert "jobs" in captured["sql"]
    assert "max(updated_at)" in captured["sql"]
    assert "status = 'done'" in captured["sql"]


def test_latest_activity_at_none_when_empty(monkeypatch):
    from app.db import clustering_queries

    class _EmptyClient:
        def execute(self, sql, *args):
            return [(None,)]

    _patch_client(monkeypatch, _EmptyClient())
    assert clustering_queries.latest_activity_at() is None


def test_latest_activity_at_none_on_error(monkeypatch):
    # Best-effort: an unreachable/absent jobs table returns None, not an error.
    from app.db import clustering_queries

    class _BoomClient:
        def execute(self, sql, *args):
            raise RuntimeError("clickhouse down")

    _patch_client(monkeypatch, _BoomClient())
    assert clustering_queries.latest_activity_at() is None
