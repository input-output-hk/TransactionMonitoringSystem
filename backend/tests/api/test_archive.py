"""Tests for the /api/archive endpoints.

The ClickHouse layer is mocked: the in-memory store below mimics a single
table keyed on (network, tx_hash) and implements just enough behaviour for
the API surface (insert / exists / get / delete / list / bulk-skip-existing /
export rows). This keeps the tests fast and DB-free while still exercising
the FastAPI plumbing end-to-end (validation, status codes, response shapes).
"""

import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeArchiveStore:
    """In-memory stand-in for the ``archived_alerts`` ClickHouse table."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Dict[str, Any]] = {}

    def reset(self) -> None:
        self.rows.clear()


@pytest.fixture
def store() -> FakeArchiveStore:
    return FakeArchiveStore()


@pytest.fixture
def client(monkeypatch, store):
    """TestClient with archive_queries.* patched against an in-memory store.

    The audit persistence is faked too: suppression endpoints are fail-closed
    on the audit write, so without a working insert_audit_log they would all
    503 (which test_audit_fail_closed.py covers explicitly).
    """
    from app.main import app
    from app.db import archive_queries, postgres

    audit_rows: List[Dict[str, Any]] = []

    async def fake_insert_audit(**kwargs):
        audit_rows.append(kwargs)
        return len(audit_rows)

    async def fake_update_audit(audit_id, outcome):
        return None

    monkeypatch.setattr(postgres, "insert_audit_log", fake_insert_audit)
    monkeypatch.setattr(postgres, "update_audit_log_details", fake_update_audit)

    async def fake_exists(network, tx_hash):
        return (network, tx_hash) in store.rows

    async def fake_insert(network, tx_hash, note, archived_by):
        store.rows[(network, tx_hash)] = {
            "network": network,
            "tx_hash": tx_hash,
            "note": note,
            "archived_by": archived_by,
            "archived_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "source": archive_queries.SOURCE_LOCAL,
        }

    async def fake_delete(network, tx_hash):
        if (network, tx_hash) in store.rows:
            del store.rows[(network, tx_hash)]
            return 1
        return 0

    async def fake_list(network, date_from=None, date_to=None, limit=100, offset=0):
        items = [
            dict(row, max_score=None, max_class=None, risk_band=None, analyzed_at=None)
            for key, row in store.rows.items()
            if key[0] == network
            and (date_from is None or row["archived_at"] >= date_from)
            and (date_to is None or row["archived_at"] <= date_to)
        ]
        items.sort(key=lambda r: r["archived_at"], reverse=True)
        return items[offset : offset + limit]

    async def fake_count(network, date_from=None, date_to=None):
        # Mirrors fake_list's filter; without this patch the list endpoint's
        # archive_count_async call reaches the real ClickHouse driver and the
        # tests stop being hermetic.
        return sum(
            1
            for key, row in store.rows.items()
            if key[0] == network
            and (date_from is None or row["archived_at"] >= date_from)
            and (date_to is None or row["archived_at"] <= date_to)
        )

    async def fake_get(network, tx_hash):
        return store.rows.get((network, tx_hash))

    async def fake_bulk_insert(entries: List[Dict[str, Any]], source_label: str):
        inserted = 0
        skipped = 0
        tag = f"{archive_queries.IMPORT_SOURCE_PREFIX}{source_label}"
        for e in entries:
            key = (e["network"], e["tx_hash"])
            if key in store.rows:
                skipped += 1
                continue
            archived_at = e.get("archived_at")
            if archived_at is None:
                archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
            elif archived_at.tzinfo is not None:
                archived_at = archived_at.astimezone(timezone.utc).replace(tzinfo=None)
            store.rows[key] = {
                "network": e["network"],
                "tx_hash": e["tx_hash"],
                "note": e["note"],
                "archived_by": e["archived_by"],
                "archived_at": archived_at,
                "source": tag,
            }
            inserted += 1
        return {"inserted": inserted, "skipped": skipped}

    async def fake_export(network, date_from=None, date_to=None):
        rows = [
            row for key, row in store.rows.items()
            if key[0] == network
            and (date_from is None or row["archived_at"] >= date_from)
            and (date_to is None or row["archived_at"] <= date_to)
        ]
        rows.sort(key=lambda r: r["archived_at"], reverse=True)
        return rows

    monkeypatch.setattr(archive_queries, "archive_exists_async", fake_exists)
    monkeypatch.setattr(archive_queries, "archive_insert_async", fake_insert)
    monkeypatch.setattr(archive_queries, "archive_delete_async", fake_delete)
    monkeypatch.setattr(archive_queries, "archive_list_async", fake_list)
    monkeypatch.setattr(archive_queries, "archive_count_async", fake_count)
    monkeypatch.setattr(archive_queries, "archive_get_async", fake_get)
    monkeypatch.setattr(archive_queries, "archive_bulk_insert_async", fake_bulk_insert)
    monkeypatch.setattr(archive_queries, "archive_export_rows_async", fake_export)

    return TestClient(app)


@pytest.fixture
def auth_open(monkeypatch):
    """Run the API in dev mode (no API-key required) for the happy-path tests."""
    from app.auth import api_key
    monkeypatch.setattr(api_key, "_valid_keys", [])
    monkeypatch.setattr(api_key, "_dev_mode", True)


VALID_HASH = "a" * 64
OTHER_HASH = "b" * 64


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_archive_then_list_then_delete(client, auth_open, store):
    r = client.post("/api/archive", json={
        "network": "preprod",
        "tx_hash": VALID_HASH,
        "note": "known FP from CTF testing",
        "archived_by": "reviewer@example.com",
    })
    assert r.status_code == 201, r.text
    assert ("preprod", VALID_HASH) in store.rows

    r = client.get("/api/archive?network=preprod")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["data"][0]["tx_hash"] == VALID_HASH
    assert body["data"][0]["note"] == "known FP from CTF testing"

    r = client.delete(f"/api/archive/{VALID_HASH}?network=preprod")
    assert r.status_code == 204

    r = client.get("/api/archive?network=preprod")
    assert r.json()["count"] == 0


def test_archive_duplicate_returns_409(client, auth_open):
    payload = {
        "network": "preprod", "tx_hash": VALID_HASH,
        "note": "n", "archived_by": "me",
    }
    assert client.post("/api/archive", json=payload).status_code == 201
    assert client.post("/api/archive", json=payload).status_code == 409


def test_delete_missing_returns_404(client, auth_open):
    r = client.delete(f"/api/archive/{VALID_HASH}?network=preprod")
    assert r.status_code == 404


def test_bulk_import_skips_existing(client, auth_open, store):
    # Pre-seed one entry locally.
    assert client.post("/api/archive", json={
        "network": "preprod", "tx_hash": VALID_HASH,
        "note": "local note", "archived_by": "local-admin",
    }).status_code == 201

    # Bulk import the same tx + one new tx.
    r = client.post("/api/archive/bulk", json={
        "source_label": "instance-b",
        "entries": [
            {
                "network": "preprod", "tx_hash": VALID_HASH,
                "note": "remote note", "archived_by": "remote-admin",
            },
            {
                "network": "preprod", "tx_hash": OTHER_HASH,
                "note": "fresh", "archived_by": "remote-admin",
            },
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"inserted": 1, "skipped": 1, "errors": []}

    # Local note was not overwritten by the remote import.
    assert store.rows[("preprod", VALID_HASH)]["note"] == "local note"
    assert store.rows[("preprod", VALID_HASH)]["source"] == "local"
    # New entry carries the import source tag.
    assert store.rows[("preprod", OTHER_HASH)]["source"] == "import:instance-b"


def test_csv_export_roundtrips_to_import(client, auth_open, store):
    # Seed two local entries.
    for h, note in ((VALID_HASH, "first"), (OTHER_HASH, "second")):
        client.post("/api/archive", json={
            "network": "preprod", "tx_hash": h,
            "note": note, "archived_by": "me",
        })
    assert len(store.rows) == 2

    # Export.
    r = client.get("/api/archive/export?network=preprod")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    text = r.text
    assert "network,tx_hash,note,archived_by,archived_at,source" in text

    # Parse CSV and feed it back as bulk import on the (already-populated)
    # store: every (network, tx_hash) is already present, so 0 inserted.
    reader = csv.DictReader(io.StringIO(text))
    entries = [
        {
            "network": row["network"], "tx_hash": row["tx_hash"],
            "note": row["note"], "archived_by": row["archived_by"],
            "archived_at": row["archived_at"] or None,
        }
        for row in reader
    ]
    r = client.post("/api/archive/bulk", json={
        "source_label": "self",
        "entries": entries,
    })
    assert r.status_code == 200
    assert r.json() == {"inserted": 0, "skipped": 2, "errors": []}


def test_csv_export_into_empty_store_imports_all(client, auth_open, store):
    """Round-trip across instances: export on A produces CSV, import on B
    (empty store) inserts all rows."""
    for h, note in ((VALID_HASH, "first"), (OTHER_HASH, "second")):
        client.post("/api/archive", json={
            "network": "preprod", "tx_hash": h,
            "note": note, "archived_by": "instance-a-admin",
        })
    csv_text = client.get("/api/archive/export?network=preprod").text

    # Simulate "instance B": wipe and re-import.
    store.reset()
    reader = csv.DictReader(io.StringIO(csv_text))
    entries = [
        {
            "network": row["network"], "tx_hash": row["tx_hash"],
            "note": row["note"], "archived_by": row["archived_by"],
            "archived_at": row["archived_at"] or None,
        }
        for row in reader
    ]
    r = client.post("/api/archive/bulk", json={
        "source_label": "instance-a",
        "entries": entries,
    })
    assert r.json() == {"inserted": 2, "skipped": 0, "errors": []}
    # archived_by from instance A is preserved; source becomes import:instance-a.
    seeded = store.rows[("preprod", VALID_HASH)]
    assert seeded["archived_by"] == "instance-a-admin"
    assert seeded["source"] == "import:instance-a"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_tx_hash_rejected(client, auth_open):
    r = client.post("/api/archive", json={
        "network": "preprod", "tx_hash": "not-a-hash",
        "note": "n", "archived_by": "me",
    })
    assert r.status_code == 422


def test_unknown_network_rejected(client, auth_open):
    r = client.post("/api/archive", json={
        "network": "testnet", "tx_hash": VALID_HASH,
        "note": "n", "archived_by": "me",
    })
    assert r.status_code == 422


def test_empty_note_rejected(client, auth_open):
    r = client.post("/api/archive", json={
        "network": "preprod", "tx_hash": VALID_HASH,
        "note": "", "archived_by": "me",
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_archive_requires_api_key_when_keys_configured(client, monkeypatch):
    from app.auth import api_key
    monkeypatch.setattr(api_key, "_valid_keys", ["sentinel-key"])
    monkeypatch.setattr(api_key, "_dev_mode", False)
    r = client.post("/api/archive", json={
        "network": "preprod", "tx_hash": VALID_HASH,
        "note": "n", "archived_by": "me",
    })
    # 401: unauthenticated (no key, no session), per verify_api_key.
    assert r.status_code == 401


def test_archive_accepts_valid_api_key(client, monkeypatch):
    from app import config
    from app.auth import api_key
    monkeypatch.setattr(api_key, "_valid_keys", ["sentinel-key"])
    monkeypatch.setattr(api_key, "_dev_mode", False)
    r = client.post(
        "/api/archive",
        json={
            "network": "preprod", "tx_hash": VALID_HASH,
            "note": "n", "archived_by": "me",
        },
        headers={config.settings.API_KEY_HEADER: "sentinel-key"},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Date-range filtering
# ---------------------------------------------------------------------------


def test_list_filters_by_date_range(client, auth_open, store):
    """The from/to query params must constrain archived_at on the list."""
    from datetime import datetime
    older = datetime(2026, 1, 1)
    newer = datetime(2026, 4, 1)
    store.rows[("preprod", VALID_HASH)] = {
        "network": "preprod", "tx_hash": VALID_HASH,
        "note": "old", "archived_by": "me",
        "archived_at": older, "source": "local",
    }
    store.rows[("preprod", OTHER_HASH)] = {
        "network": "preprod", "tx_hash": OTHER_HASH,
        "note": "new", "archived_by": "me",
        "archived_at": newer, "source": "local",
    }

    # Window that includes only the newer row.
    r = client.get(
        "/api/archive?network=preprod"
        "&from=2026-03-01T00:00:00Z&to=2026-12-31T23:59:59Z"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["data"][0]["tx_hash"] == OTHER_HASH

    # No bounds => both rows.
    assert client.get("/api/archive?network=preprod").json()["count"] == 2


def test_export_filters_by_date_range(client, auth_open, store):
    """CSV export must honour the same from/to window as the list endpoint."""
    from datetime import datetime
    store.rows[("preprod", VALID_HASH)] = {
        "network": "preprod", "tx_hash": VALID_HASH,
        "note": "old", "archived_by": "me",
        "archived_at": datetime(2026, 1, 1), "source": "local",
    }
    store.rows[("preprod", OTHER_HASH)] = {
        "network": "preprod", "tx_hash": OTHER_HASH,
        "note": "new", "archived_by": "me",
        "archived_at": datetime(2026, 4, 1), "source": "local",
    }
    r = client.get(
        "/api/archive/export?network=preprod"
        "&from=2026-03-01T00:00:00Z&to=2026-12-31T23:59:59Z"
    )
    assert r.status_code == 200
    body = r.text
    assert OTHER_HASH in body
    assert VALID_HASH not in body


# ---------------------------------------------------------------------------
# In-batch dedup (regression guard for M1)
# ---------------------------------------------------------------------------


def test_bulk_import_dedupes_within_batch(client, auth_open, store):
    """The same (network, tx_hash) repeated in a single batch must be counted
    as one insert; the duplicates show up as skipped."""
    payload = {
        "source_label": "instance-x",
        "entries": [
            {
                "network": "preprod", "tx_hash": VALID_HASH,
                "note": "first occurrence", "archived_by": "x",
            },
            {
                "network": "preprod", "tx_hash": VALID_HASH,
                "note": "duplicate, should be ignored", "archived_by": "x",
            },
            {
                "network": "preprod", "tx_hash": VALID_HASH,
                "note": "another dup", "archived_by": "x",
            },
        ],
    }
    r = client.post("/api/archive/bulk", json=payload)
    assert r.status_code == 200
    assert r.json() == {"inserted": 1, "skipped": 2, "errors": []}
    # Only one row in the store, with the FIRST occurrence's note preserved.
    assert len(store.rows) == 1
    assert store.rows[("preprod", VALID_HASH)]["note"] == "first occurrence"


# ---------------------------------------------------------------------------
# Suppression contract on /api/analysis (regression guard for M2)
# ---------------------------------------------------------------------------


def test_class_scores_list_sql_excludes_archived_by_default():
    """Direct unit test on the SQL builder: by default the WHERE clause must
    anti-join against archived_alerts so admin-curated FPs are dropped from
    the dangerous-transactions list. Future refactors of the query path
    must keep this filter or this test fails."""
    from unittest.mock import patch
    from app.db import clickhouse

    captured = {}

    class FakeClient:
        def execute(self, query, params=None):
            captured.setdefault("queries", []).append((query, params or {}))
            # Return shape: zero rows on the score select, zero on the
            # transactions-detail follow-up.
            return []

    with patch("app.db.clickhouse._get_client", return_value=FakeClient()):
        clickhouse.get_class_scores_list(network="preprod")

    score_sql = captured["queries"][0][0]
    assert "archived_alerts" in score_sql, (
        "get_class_scores_list must filter against archived_alerts when "
        "include_archived=False (default), otherwise admin archives have "
        "no effect on /api/analysis/results"
    )
    assert "NOT IN" in score_sql


def test_class_scores_list_sql_includes_archived_when_requested():
    """The opt-out switch must remove the anti-join."""
    from unittest.mock import patch
    from app.db import clickhouse

    captured = {}

    class FakeClient:
        def execute(self, query, params=None):
            captured.setdefault("queries", []).append(query)
            return []

    with patch("app.db.clickhouse._get_client", return_value=FakeClient()):
        clickhouse.get_class_scores_list(network="preprod", include_archived=True)

    assert "archived_alerts" not in captured["queries"][0]


def test_count_class_scores_sql_excludes_archived_by_default():
    """count_class_scores shares the list query's WHERE builder, so it must
    apply the same archive anti-join — otherwise pagination totals would count
    archived rows the list itself drops."""
    from unittest.mock import patch
    from app.db import clickhouse

    captured = {"queries": []}

    class FakeClient:
        def execute(self, query, params=None):
            captured["queries"].append(query)
            return [(0,)]

    with patch("app.db.clickhouse._get_client", return_value=FakeClient()):
        clickhouse.count_class_scores(
            network="preprod", risk_band=None, attack_class=None, min_score=0.0,
        )

    count_sql = captured["queries"][0]
    assert "archived_alerts" in count_sql
    assert "NOT IN" in count_sql


def test_count_class_scores_sql_includes_archived_when_requested():
    from unittest.mock import patch
    from app.db import clickhouse

    captured = {"queries": []}

    class FakeClient:
        def execute(self, query, params=None):
            captured["queries"].append(query)
            return [(0,)]

    with patch("app.db.clickhouse._get_client", return_value=FakeClient()):
        clickhouse.count_class_scores(
            network="preprod", risk_band=None, attack_class=None, min_score=0.0,
            include_archived=True,
        )

    assert "archived_alerts" not in captured["queries"][0]


def test_class_scores_stats_sql_excludes_archived_by_default():
    """Same contract for /api/analysis/stats: band counts must not include
    archived transactions."""
    from unittest.mock import patch
    from app.db import clickhouse

    captured = {"queries": []}

    class FakeClient:
        def execute(self, query, params=None):
            # Capture every query: get_class_scores_stats issues the stats query
            # first, then get_pending_count issues its own. Asserting on the
            # stats query (the first) avoids checking the unrelated pending one.
            captured["queries"].append(query)
            # Return a single row matching the schema the parser expects.
            zero_per_class = [0, 0.0, 0.0] * 9
            return [(0, 0, 0, 0, 0, 0.0, None, *zero_per_class)]

    with patch("app.db.clickhouse._get_client", return_value=FakeClient()):
        clickhouse.get_class_scores_stats(network="preprod")

    stats_query = captured["queries"][0]
    assert "archived_alerts" in stats_query
    assert "NOT IN" in stats_query


def test_class_scores_stats_sql_skips_filter_when_include_archived():
    from unittest.mock import patch
    from app.db import clickhouse

    captured = {"queries": []}

    class FakeClient:
        def execute(self, query, params=None):
            captured["queries"].append(query)
            zero_per_class = [0, 0.0, 0.0] * 9
            return [(0, 0, 0, 0, 0, 0.0, None, *zero_per_class)]

    with patch("app.db.clickhouse._get_client", return_value=FakeClient()):
        clickhouse.get_class_scores_stats(network="preprod", include_archived=True)

    # The stats query (first) must omit the archive clause; the later
    # get_pending_count query is unrelated.
    assert "archived_alerts" not in captured["queries"][0]


class TestCsvInjectionNeutralization:
    def test_formula_prefixes_are_quoted(self):
        from app.api.archive import _csv_safe
        for payload in ("=HYPERLINK(\"//evil\")", "+1+1", "-2+3", "@SUM(A1)", "\ttab", "\rcr"):
            out = _csv_safe(payload)
            assert out.startswith("'"), payload
        # Benign values are unchanged.
        assert _csv_safe("just a note") == "just a note"
        assert _csv_safe("addr_test1q...") == "addr_test1q..."
        assert _csv_safe(None) == ""
