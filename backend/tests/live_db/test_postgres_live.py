"""Live-Postgres smoke tests: real schema plus representative queries.

Covers the paths the hermetic suite only sees through mocks: idempotent
schema DDL, the lifecycle upsert state machine, entity state, and the
sync checkpoint. All rows live under the LIVE_NETWORK namespace and are
removed afterwards. Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid
from datetime import datetime, timezone

from app.db import postgres
from app.db.postgres import get_connection

from .conftest import LIVE_NETWORK


async def _rm_lifecycle(tx_id: str) -> None:
    async with get_connection() as conn:
        await conn.execute("DELETE FROM tx_lifecycle WHERE tx_id = $1", tx_id)


class TestSchema:
    def test_schema_reapplies_idempotently(self, pg_run):
        # The fixture already applied it once; a second pass must be a
        # no-op, not a DDL error (the app runs this on every boot).
        async def scenario():
            await postgres.execute_schema()
            from app.auth.schema import execute_auth_schema

            await execute_auth_schema()

        pg_run(scenario)


class TestLifecycle:
    def test_pending_to_confirmed_and_summary(self, pg_run):
        async def scenario():
            tx_id = f"livetest-{uuid.uuid4().hex}"
            now = datetime.now(timezone.utc)
            try:
                await postgres.upsert_lifecycle_pending(tx_id, LIVE_NETWORK, first_seen_at=now)
                pending = await postgres.get_lifecycle_by_tx_id(tx_id)
                assert pending is not None
                assert pending["status"] == "PENDING"

                await postgres.upsert_lifecycle_confirmed(
                    tx_id,
                    LIVE_NETWORK,
                    confirmed_at=now,
                    block_hash="ab" * 32,
                    slot=1_000_000,
                    height=500_000,
                )
                confirmed = await postgres.get_lifecycle_by_tx_id(tx_id)
                assert confirmed["status"] == "CONFIRMED"

                summary = await postgres.get_lifecycle_summary(LIVE_NETWORK)
                assert summary["confirmed_count"] >= 1
                listed = await postgres.get_lifecycles_by_status("CONFIRMED", LIVE_NETWORK)
                assert any(r["tx_id"] == tx_id for r in listed)
            finally:
                await _rm_lifecycle(tx_id)

        pg_run(scenario)


class TestEntityState:
    def test_set_then_get_roundtrip(self, pg_run):
        async def scenario():
            entity_id = f"live-{uuid.uuid4().hex[:16]}"
            try:
                await postgres.set_entity_state(
                    "wallet", entity_id, {"flagged": True}, LIVE_NETWORK
                )
                # Returns the parsed JSON state itself, not a row wrapper.
                state = await postgres.get_entity_state("wallet", entity_id, LIVE_NETWORK)
                assert state == {"flagged": True}
            finally:
                async with get_connection() as conn:
                    await conn.execute(
                        "DELETE FROM entity_state WHERE entity_id = $1",
                        entity_id,
                    )

        pg_run(scenario)


class TestSyncCheckpoint:
    def test_save_then_get_roundtrip(self, pg_run):
        async def scenario():
            try:
                await postgres.save_sync_point(LIVE_NETWORK, 12_345, "cd" * 32)
                point = await postgres.get_sync_point(LIVE_NETWORK)
                assert point is not None
                assert point["slot"] == 12_345
            finally:
                async with get_connection() as conn:
                    await conn.execute(
                        "DELETE FROM sync_checkpoint WHERE network = $1",
                        LIVE_NETWORK,
                    )

        pg_run(scenario)
