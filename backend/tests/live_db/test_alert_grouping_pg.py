"""Live-Postgres tests for the group-alert dedup ledger.

The two recall guards on alert grouping live in SQL, not Python: band
escalation is the `band_rank >= $4` predicate and window expiry is the
`notified_at > NOW() - interval` predicate in
:func:`app.db.postgres.already_notified_group`. The hermetic tier can only pin
the short-circuits around them, so a mocked suite would stay green if either
predicate were wrong or dropped, which is exactly how a real suppression bug
would reach production. These exercise the statements against a live server.

All rows use throwaway UUID group keys under LIVE_NETWORK and are removed
afterwards. Requires TMS_LIVE_DB_TESTS=1 (see conftest).
"""

import uuid

from app.db import postgres
from app.db.postgres import get_connection

from .conftest import LIVE_NETWORK

# Long enough that nothing expires mid-test.
WINDOW_MINUTES = 60


async def _rm_group(group_key: str) -> None:
    async with get_connection() as conn:
        await conn.execute("DELETE FROM notified_alert_groups WHERE group_key = $1", group_key)


async def _age_group(group_key: str, minutes: int) -> None:
    """Backdate a claim so the window predicate sees it as expired."""
    async with get_connection() as conn:
        await conn.execute(
            """
            UPDATE notified_alert_groups
            SET notified_at = NOW() - ($2 * INTERVAL '1 minute')
            WHERE group_key = $1
            """,
            group_key,
            minutes,
        )


class TestGroupDedup:
    def test_first_claim_then_same_band_suppresses(self, pg_run):
        async def scenario():
            key = f"large_datum:{uuid.uuid4().hex}"
            try:
                # Nothing claimed yet: must not suppress.
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Critical", WINDOW_MINUTES
                    )
                    is False
                )
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Critical")
                # The burst case: same group, same band, inside the window.
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Critical", WINDOW_MINUTES
                    )
                    is True
                )
            finally:
                await _rm_group(key)

        pg_run(scenario)

    def test_escalation_to_a_higher_band_is_never_suppressed(self, pg_run):
        async def scenario():
            key = f"large_datum:{uuid.uuid4().hex}"
            try:
                await postgres.claim_notification_group(LIVE_NETWORK, key, "High")
                # Same window, but this is worse than what we alerted on. The
                # whole point of band_rank: a group that has only produced Highs
                # must still page on its first Critical, immediately.
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Critical", WINDOW_MINUTES
                    )
                    is False
                )
                # A lower band inside the window stays suppressed.
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Moderate", WINDOW_MINUTES
                    )
                    is True
                )
            finally:
                await _rm_group(key)

        pg_run(scenario)

    def test_claim_does_not_lower_an_existing_higher_band(self, pg_run):
        async def scenario():
            key = f"large_datum:{uuid.uuid4().hex}"
            try:
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Critical")
                # GREATEST() in the upsert: a later Moderate delivery must not
                # lower the bar, or the next Critical would be suppressed.
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Moderate")
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Critical", WINDOW_MINUTES
                    )
                    is True
                )
            finally:
                await _rm_group(key)

        pg_run(scenario)

    def test_suppression_expires_with_the_window(self, pg_run):
        async def scenario():
            key = f"large_datum:{uuid.uuid4().hex}"
            try:
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Critical")
                await _age_group(key, WINDOW_MINUTES + 1)
                # Past the window: a persistent condition re-alerts rather than
                # going quiet forever. This is what bounds the feature.
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Critical", WINDOW_MINUTES
                    )
                    is False
                )
            finally:
                await _rm_group(key)

        pg_run(scenario)

    def test_claim_refreshes_the_window(self, pg_run):
        async def scenario():
            key = f"large_datum:{uuid.uuid4().hex}"
            try:
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Critical")
                await _age_group(key, WINDOW_MINUTES + 1)
                # Re-delivering after expiry restarts the window.
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Critical")
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK, key, "Critical", WINDOW_MINUTES
                    )
                    is True
                )
            finally:
                await _rm_group(key)

        pg_run(scenario)

    def test_groups_are_network_and_source_scoped(self, pg_run):
        async def scenario():
            key = f"large_datum:{uuid.uuid4().hex}"
            try:
                await postgres.claim_notification_group(LIVE_NETWORK, key, "Critical")
                # A claim on one network must not suppress another's alerts.
                assert (
                    await postgres.already_notified_group(
                        f"{LIVE_NETWORK}-other", key, "Critical", WINDOW_MINUTES
                    )
                    is False
                )
                # Nor across dedup streams: scorer and contract_anomaly are
                # independent detections of the same thing.
                assert (
                    await postgres.already_notified_group(
                        LIVE_NETWORK,
                        key,
                        "Critical",
                        WINDOW_MINUTES,
                        source="contract_anomaly",
                    )
                    is False
                )
            finally:
                await _rm_group(key)

        pg_run(scenario)

    def test_prune_removes_aged_rows_only(self, pg_run):
        async def scenario():
            fresh = f"large_datum:{uuid.uuid4().hex}"
            aged = f"large_datum:{uuid.uuid4().hex}"
            try:
                await postgres.claim_notification_group(LIVE_NETWORK, fresh, "Critical")
                await postgres.claim_notification_group(LIVE_NETWORK, aged, "Critical")
                await _age_group(aged, 10 * 24 * 60)  # 10 days
                await postgres.prune_notified_alert_groups(7)
                async with get_connection() as conn:
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM notified_alert_groups WHERE group_key = ANY($1)",
                        [fresh, aged],
                    )
                assert remaining == 1
            finally:
                await _rm_group(fresh)
                await _rm_group(aged)

        pg_run(scenario)
