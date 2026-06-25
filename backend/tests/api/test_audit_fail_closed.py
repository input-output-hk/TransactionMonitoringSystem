"""Fail-closed audit contract for the alert-suppression endpoints.

Alert suppression is the highest-impact mutation in a monitoring system: an
attacker who could force the audit write to fail (review finding: a forged
X-Forwarded-For aborting the ::inet cast) must not be able to hide a
detection silently. These tests pin: audit failure -> 503 AND no mutation;
the intent row is written BEFORE the mutation; non-suppression audit events
stay best-effort.
"""

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

VALID_HASH = "a" * 64


class AuditLog:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.outcomes: List[tuple] = []
        self.fail = False
        self.call_order: List[str] = []


@pytest.fixture
def audit_log() -> AuditLog:
    return AuditLog()


@pytest.fixture
def client(monkeypatch, audit_log):
    """TestClient with audit persistence + archive store faked, recording
    the relative order of audit writes and archive mutations."""
    from app.main import app
    from app.db import archive_queries, postgres

    store: Dict[tuple, Dict[str, Any]] = {}

    async def fake_insert_audit(**kwargs):
        if audit_log.fail:
            raise RuntimeError("postgres down")
        audit_log.call_order.append("audit")
        audit_log.rows.append(kwargs)
        return len(audit_log.rows)

    async def fake_update_audit(audit_id, outcome):
        audit_log.outcomes.append((audit_id, outcome))

    async def fake_exists(network, tx_hash):
        return (network, tx_hash) in store

    async def fake_insert(network, tx_hash, note, archived_by):
        audit_log.call_order.append("mutate")
        store[(network, tx_hash)] = {"note": note}

    async def fake_delete(network, tx_hash):
        audit_log.call_order.append("mutate")
        return 1 if store.pop((network, tx_hash), None) else 0

    async def fake_bulk_insert(entries, source_label):
        audit_log.call_order.append("mutate")
        for e in entries:
            store[(e["network"], e["tx_hash"])] = {"note": e["note"]}
        return {"inserted": len(entries), "skipped": 0}

    monkeypatch.setattr(postgres, "insert_audit_log", fake_insert_audit)
    monkeypatch.setattr(postgres, "update_audit_log_details", fake_update_audit)
    monkeypatch.setattr(archive_queries, "archive_exists_async", fake_exists)
    monkeypatch.setattr(archive_queries, "archive_insert_async", fake_insert)
    monkeypatch.setattr(archive_queries, "archive_delete_async", fake_delete)
    monkeypatch.setattr(
        archive_queries, "archive_bulk_insert_async", fake_bulk_insert,
    )

    client = TestClient(app)
    client._store = store  # type: ignore[attr-defined]
    return client


@pytest.fixture
def auth_open(monkeypatch):
    from app.auth import api_key
    monkeypatch.setattr(api_key, "_valid_keys", [])
    monkeypatch.setattr(api_key, "_dev_mode", True)


def _archive_payload():
    return {
        "network": "preprod",
        "tx_hash": VALID_HASH,
        "note": "fp",
        "archived_by": "tester",
    }


class TestAuditFailureRefusesSuppression:
    def test_archive_503_and_no_row(self, client, audit_log, auth_open):
        audit_log.fail = True
        resp = client.post("/api/archive", json=_archive_payload())
        assert resp.status_code == 503
        assert client._store == {}

    def test_bulk_503_and_no_rows(self, client, audit_log, auth_open):
        audit_log.fail = True
        resp = client.post(
            "/api/archive/bulk",
            json={"entries": [_archive_payload()], "source_label": "batch"},
        )
        assert resp.status_code == 503
        assert client._store == {}

    def test_restore_503_and_row_kept(self, client, audit_log, auth_open):
        client._store[("preprod", VALID_HASH)] = {"note": "fp"}
        audit_log.fail = True
        resp = client.delete(f"/api/archive/{VALID_HASH}?network=preprod")
        assert resp.status_code == 503
        assert ("preprod", VALID_HASH) in client._store


class TestIntentBeforeMutation:
    def test_audit_written_before_archive_insert(
        self, client, audit_log, auth_open
    ):
        resp = client.post("/api/archive", json=_archive_payload())
        assert resp.status_code == 201
        assert audit_log.call_order == ["audit", "mutate"]
        assert audit_log.rows[0]["event_type"] == "alert_suppression"
        # Outcome patched in after the mutation succeeded.
        assert audit_log.outcomes and '"applied"' in audit_log.outcomes[0][1]

    def test_failed_mutation_patches_failed_outcome(
        self, client, audit_log, auth_open, monkeypatch
    ):
        from app.db import archive_queries

        async def broken_insert(network, tx_hash, note, archived_by):
            raise RuntimeError("clickhouse down")

        monkeypatch.setattr(
            archive_queries, "archive_insert_async", broken_insert,
        )
        resp = client.post("/api/archive", json=_archive_payload())
        assert resp.status_code == 500
        # The intent row exists with phase=failed: visible, not silent.
        assert audit_log.rows
        assert audit_log.outcomes and '"failed"' in audit_log.outcomes[0][1]


class TestSpoofedHeaderCannotBreakAudit:
    def test_malformed_xff_is_sanitized_end_to_end(
        self, client, audit_log, auth_open, monkeypatch
    ):
        from app.config import settings

        monkeypatch.setattr(settings, "TRUSTED_PROXY_ENABLED", True)
        resp = client.post(
            "/api/archive",
            json=_archive_payload(),
            headers={"X-Forwarded-For": "notanip"},
        )
        # The forged header degrades to the (validated) direct peer instead
        # of aborting the ::inet cast and blocking the audit write.
        assert resp.status_code == 201
        ip = audit_log.rows[0]["ip_address"]
        assert ip is None or "." in ip or ":" in ip


class TestActorIsAuthenticatedPrincipal:
    """The audit actor is the server-derived authenticated principal, never
    the spoofable ``archived_by`` request field (review finding)."""

    def test_actor_is_server_principal_not_client_field(
        self, client, audit_log, auth_open
    ):
        import json

        payload = _archive_payload()
        payload["archived_by"] = "attacker-spoofed-name"
        resp = client.post("/api/archive", json=payload)
        assert resp.status_code == 201
        details = json.loads(audit_log.rows[0]["details"])
        # dev-mode principal -> actor "dev-mode"; the client label is kept
        # separately and is NOT what attributes the mutation.
        assert details["actor"] == "dev-mode"
        assert details["archived_by"] == "attacker-spoofed-name"

    def test_api_key_actor_is_fingerprint_not_raw_key(
        self, client, audit_log, monkeypatch
    ):
        import json

        from app import audit
        from app.auth import api_key
        from app.config import settings

        key = "super-secret-key-value"
        monkeypatch.setattr(api_key, "_valid_keys", [key])
        monkeypatch.setattr(api_key, "_dev_mode", False)
        resp = client.post(
            "/api/archive",
            json=_archive_payload(),
            headers={settings.API_KEY_HEADER: key},
        )
        assert resp.status_code == 201
        details = json.loads(audit_log.rows[0]["details"])
        assert details["actor"] == audit.actor_from_principal(key)
        assert details["actor"].startswith("api-key:")
        # The secret itself must never appear anywhere in the audit row.
        assert key not in audit_log.rows[0]["details"]


class TestNonSuppressionStaysBestEffort:
    def test_entity_state_write_survives_audit_outage(
        self, client, audit_log, auth_open, monkeypatch
    ):
        from app.db import postgres

        async def fake_set(entity_type, entity_id, state, network):
            return None

        monkeypatch.setattr(postgres, "set_entity_state", fake_set)
        audit_log.fail = True
        resp = client.put(
            "/api/entities/address/addr_test1xyz", json={"label": "x"},
        )
        assert resp.status_code == 200
