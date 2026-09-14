"""Live-Postgres tests for the failed-notification dead letter.

The sweep's correctness lives in SQL, not Python: the due predicate is the
``last_attempt_at + make_interval(...)`` backoff arithmetic, escalation is the
``band_rank < EXCLUDED.band_rank`` upsert guard, and retention is scoped to
abandoned rows only. The hermetic tier mocks all of these away, so a wrong
predicate would stay green there and lose real alerts in production. These
exercise the statements against a live server.

All rows use throwaway UUID tx hashes under LIVE_NETWORK and are removed
afterwards. Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid

from app.db import postgres
from app.db.postgres import get_connection

from .conftest import LIVE_NETWORK

# Base delay large enough that a just-written row is never due mid-test.
SLOW_BACKOFF_SECONDS = 3600.0
DUE_LIMIT = 50

PAYLOAD = {"tx_hash": "x", "attack_class": "phishing", "risk_band": "Critical"}


def _tx() -> str:
    return f"dead-letter-{uuid.uuid4().hex}"


async def _rm(tx_hash: str) -> None:
    async with get_connection() as conn:
        await conn.execute("DELETE FROM failed_notifications WHERE tx_hash = $1", tx_hash)


async def _backdate(tx_hash: str, seconds: int) -> None:
    """Age a row so the backoff predicate sees it as due."""
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE failed_notifications
            SET last_attempt_at = NOW() - ($2 * INTERVAL '1 second')
            WHERE tx_hash = $1
            """,
            tx_hash,
            seconds,
        )


async def _due_hashes(base: float = SLOW_BACKOFF_SECONDS) -> set[str]:
    rows = await postgres.due_failed_notifications(base, DUE_LIMIT)
    return {r["tx_hash"] for r in rows}


class TestDeadLetter:
    def test_backoff_gates_dueness_and_payload_round_trips(self, pg_run):
        async def scenario():
            tx = _tx()
            try:
                await postgres.record_failed_notification(
                    LIVE_NETWORK, tx, "Critical", PAYLOAD, group_key="g1"
                )
                # Fresh row: the base backoff has not elapsed.
                assert tx not in await _due_hashes()
                await _backdate(tx, int(SLOW_BACKOFF_SECONDS) + 60)
                due = [
                    r
                    for r in await postgres.due_failed_notifications(
                        SLOW_BACKOFF_SECONDS, DUE_LIMIT
                    )
                    if r["tx_hash"] == tx
                ]
                assert len(due) == 1
                # The payload comes back as the dict that was stored (the sweep
                # re-sends it verbatim), with the routing context alongside.
                assert due[0]["payload"] == PAYLOAD
                assert due[0]["band"] == "Critical"
                assert due[0]["group_key"] == "g1"
                assert due[0]["attempts"] == 1
            finally:
                await _rm(tx)

        pg_run(scenario)

    def test_backoff_doubles_with_attempts(self, pg_run):
        async def scenario():
            tx = _tx()
            try:
                await postgres.record_failed_notification(LIVE_NETWORK, tx, "Critical", PAYLOAD)
                await postgres.mark_failed_attempt(LIVE_NETWORK, tx, "scorer", abandoned=False)
                # attempts=2: due after base * 2^1, not after base alone.
                await _backdate(tx, int(SLOW_BACKOFF_SECONDS) + 60)
                assert tx not in await _due_hashes()
                await _backdate(tx, 2 * int(SLOW_BACKOFF_SECONDS) + 60)
                assert tx in await _due_hashes()
            finally:
                await _rm(tx)

        pg_run(scenario)

    def test_escalation_replaces_and_lower_band_does_not(self, pg_run):
        async def scenario():
            tx = _tx()
            try:
                await postgres.record_failed_notification(
                    LIVE_NETWORK, tx, "High", {**PAYLOAD, "risk_band": "High"}
                )
                # Same-band re-failure: a no-op, the attempt counter is the
                # sweep's alone.
                await postgres.mark_failed_attempt(LIVE_NETWORK, tx, "scorer", abandoned=False)
                await postgres.record_failed_notification(
                    LIVE_NETWORK, tx, "High", {**PAYLOAD, "risk_band": "High"}
                )
                await _backdate(tx, 10 * int(SLOW_BACKOFF_SECONDS))
                (row,) = [
                    r
                    for r in await postgres.due_failed_notifications(
                        SLOW_BACKOFF_SECONDS, DUE_LIMIT
                    )
                    if r["tx_hash"] == tx
                ]
                assert row["attempts"] == 2

                # Escalation: the Critical failure replaces the pending High row.
                await postgres.record_failed_notification(
                    LIVE_NETWORK, tx, "Critical", {**PAYLOAD, "risk_band": "Critical"}
                )
                await _backdate(tx, 10 * int(SLOW_BACKOFF_SECONDS))
                (row,) = [
                    r
                    for r in await postgres.due_failed_notifications(
                        SLOW_BACKOFF_SECONDS, DUE_LIMIT
                    )
                    if r["tx_hash"] == tx
                ]
                assert row["band"] == "Critical"
                assert row["payload"]["risk_band"] == "Critical"
            finally:
                await _rm(tx)

        pg_run(scenario)

    def test_abandoned_rows_are_not_due_and_only_they_prune(self, pg_run):
        async def scenario():
            tx_live, tx_dead = _tx(), _tx()
            try:
                await postgres.record_failed_notification(
                    LIVE_NETWORK, tx_live, "Critical", PAYLOAD
                )
                await postgres.record_failed_notification(
                    LIVE_NETWORK, tx_dead, "Critical", PAYLOAD
                )
                await postgres.mark_failed_attempt(LIVE_NETWORK, tx_dead, "scorer", abandoned=True)
                await _backdate(tx_live, 10 * int(SLOW_BACKOFF_SECONDS))
                await _backdate(tx_dead, 10 * int(SLOW_BACKOFF_SECONDS))
                due = await _due_hashes()
                assert tx_live in due and tx_dead not in due
                # Retention removes only the abandoned row, however old the
                # live one is: a live row still carries a pending retry.
                await postgres.prune_failed_notifications(0)
                async with get_connection() as conn:
                    remaining = {
                        r["tx_hash"]
                        for r in await conn.fetch(
                            "SELECT tx_hash FROM failed_notifications WHERE tx_hash = ANY($1)",
                            [tx_live, tx_dead],
                        )
                    }
                assert remaining == {tx_live}
            finally:
                await _rm(tx_live)
                await _rm(tx_dead)

        pg_run(scenario)

    def test_delete_removes_the_row(self, pg_run):
        async def scenario():
            tx = _tx()
            await postgres.record_failed_notification(LIVE_NETWORK, tx, "Critical", PAYLOAD)
            await postgres.delete_failed_notification(LIVE_NETWORK, tx, "scorer")
            await _backdate(tx, 10 * int(SLOW_BACKOFF_SECONDS))
            assert tx not in await _due_hashes()

        pg_run(scenario)
