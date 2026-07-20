"""Live validation of the history-backfill query text (ClickHouse 26.x).

Every query here is NEW text introduced with the pre-deployment history
backfill and is otherwise only pin-tested against fakes: the hybrid repo's
host-UNION-local reads (type unification between the host's String and the
module's FixedString hashes, alias resolution, the windowed LIMIT), the
``host_history_boundary`` aggregates, the ``host_known_tx_hashes`` publish
bound, and the source-tagged ``ingest_cursor`` round trip. A fake client
cannot catch server-side rejection of any of them.

The local arm is seeded through the module's own insert path under a
throwaway UUID target; the host arm stays empty (reads filter on this tier's
own network namespace, which nothing else ever writes), which still forces
the server to parse, type-unify and execute the full union text.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.models import TxRecord, UtxoRecord
from app.service.history import host_history_boundary
from app.storage.clickhouse import ClickHouseRepo
from app.storage.clickhouse.base import connect
from app.storage.clickhouse.hybrid import HybridHistoryRepo

from .conftest import LIVE_NETWORK


def _live_settings() -> Settings:
    return Settings(
        CHAIN_SOURCE="host_ch",
        HISTORY_SOURCE="blockfrost",
        BLOCKFROST_PROJECT_ID="livedbtest-key",
        CARDANO_NETWORK=LIVE_NETWORK,
        CLICKHOUSE_HOST=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        CLICKHOUSE_HTTP_PORT=int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")),
        CLICKHOUSE_USER=os.environ.get("CLICKHOUSE_USER", "default"),
        CLICKHOUSE_PASSWORD=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        CLICKHOUSE_DB=os.environ.get("CLICKHOUSE_DB", "tms_clustering"),
        HOST_CLICKHOUSE_DB=os.environ.get("HOST_CLICKHOUSE_DB", "tms_analytics"),
        CLUSTERING_WINDOW_TXS=100,
    )


def _tx(target: str, tx_hash: str, slot: int, height: int) -> TxRecord:
    return TxRecord(
        target=target,
        target_type="address",
        tx_hash=tx_hash,
        block_height=height,
        block_time=datetime(2023, 11, 14, tzinfo=UTC),
        slot=slot,
        fees=200_000,
        deposit=0,
        size=300,
        valid_contract=1,
        input_count=1,
        output_count=2,
        total_input_lovelace=1_000_000,
        total_output_lovelace=900_000,
        distinct_input_addresses=1,
        distinct_output_addresses=2,
        distinct_assets=0,
        redeemer_count=0,
    )


@pytest.fixture(scope="module")
def seeded() -> tuple[Settings, str, list[str]]:
    """Three local-history rows under a throwaway target, written through the
    module's own insert path (module-scoped: one seed serves every test)."""
    settings = _live_settings()
    target = f"livedbtest_{uuid.uuid4().hex[:16]}"
    hashes = [f"{i:02x}" * 32 for i in (0xAA, 0xBB, 0xCC)]
    repo = ClickHouseRepo(settings)
    try:
        repo.insert_transactions(
            [_tx(target, h, slot=100 * (i + 1), height=10 * (i + 1)) for i, h in enumerate(hashes)]
        )
        repo.insert_utxos(
            [
                UtxoRecord(
                    target=target,
                    tx_hash=h,
                    role="output",
                    idx=0,
                    address=f"addr_{target}",
                    lovelace=1,
                )
                for h in hashes
            ]
        )
    finally:
        repo.close()
    return settings, target, hashes


def test_hybrid_union_reads_execute_live(seeded: tuple[Settings, str, list[str]]) -> None:
    settings, target, hashes = seeded
    repo = HybridHistoryRepo(settings)
    try:
        # Windowed hash set + count over the union (LIMIT, alias s/s2, GROUP BY).
        assert repo.count_transactions(target) == len(hashes)
        # Engine-shaped columns over the union: FixedString→String unification,
        # the local arm's net_lovelace cast, the distinct_assets sub-union.
        df = repo.fetch_shape_features(target)
        assert len(df) == len(hashes)
        assert int(df["net_lovelace"].iloc[0]) == 900_000 - 1_000_000
        # By-hash variant (the NOT IN host-precedence guard parses and runs).
        assert len(repo.fetch_shape_features_for(target, hashes[:1])) == 1
        # Address co-occurrence: triple UNION DISTINCT across host inputs,
        # host outputs and local tx_utxos.
        addrs = repo.fetch_addresses_for_txs(target, hashes)
        assert set(addrs["address"]) == {f"addr_{target}"}
        # Windowed co-occurrence variant (INNER JOIN over the union window).
        assert len(repo.fetch_tx_addresses(target)) == len(hashes)
        # The local-history count reads only the module table.
        assert repo.history_tx_count(target) == len(hashes)
    finally:
        repo.close()


def test_host_membership_bound_executes_live(seeded: tuple[Settings, str, list[str]]) -> None:
    # The publish bound queries the HOST index only: the seeded local rows must
    # NOT count as host-known (that is what keeps history out of alerting).
    settings, target, hashes = seeded
    repo = HybridHistoryRepo(settings)
    try:
        assert repo.host_known_tx_hashes(target, set(hashes)) == set()
    finally:
        repo.close()


def test_host_membership_bound_matches_a_seeded_host_row() -> None:
    # The sibling test above only proves the query correctly returns EMPTY
    # against a live server; it never proves the query's IN {hs:Array(String)}
    # clause correctly returns a MATCH. Given this project's history of
    # live-only ClickHouse bugs mocks cannot catch (parameter binding, type
    # coercion), an undetected defect here would silently zero out ALL
    # contract_anomaly publishing — this seeds a real row directly into the
    # HOST's own address_transactions table (this tier's one intentional
    # exception to "nothing is ever written to the host tables") to close
    # that gap.
    settings = _live_settings()
    target = f"livedbtest_hostmatch_{uuid.uuid4().hex[:16]}"
    host_known, local_only = f"{0xAA:02x}" * 32, f"{0xBB:02x}" * 32
    client = connect(settings, database=settings.host_clickhouse_db)
    try:
        client.insert(
            "address_transactions",
            [
                [
                    LIVE_NETWORK,
                    target,
                    100,
                    host_known,
                    datetime(2023, 11, 14, tzinfo=UTC),
                    datetime.now(UTC),
                ]
            ],
            column_names=[
                "network",
                "address",
                "slot",
                "tx_hash",
                "timestamp",
                "ingestion_timestamp",
            ],
        )
    finally:
        client.close()

    repo = HybridHistoryRepo(settings)
    try:
        assert repo.host_known_tx_hashes(target, {host_known, local_only}) == {host_known}
    finally:
        repo.close()


def test_boundary_aggregates_execute_live(seeded: tuple[Settings, str, list[str]]) -> None:
    # Empty host network → no tip → defer (None). The point is that all three
    # aggregates (tip max, minIf floor + uniqExact, height floor with the IN
    # subquery) parse and execute on the live server.
    settings, target, _hashes = seeded
    assert host_history_boundary(settings, target) is None


def test_source_tagged_cursor_round_trips_live(seeded: tuple[Settings, str, list[str]]) -> None:
    # The source-tagged upsert must land in the live ingest_cursor schema and
    # read back verbatim (catches a missing/renamed column that fakes cannot).
    settings, target, _hashes = seeded
    repo = ClickHouseRepo(settings)
    try:
        repo.upsert_cursor(
            target,
            "address",
            cursor="page:5;from:100",
            last_tx_hash="",
            txs_seen=3,
            done=False,
            source="blockfrost_history",
        )
        cur = repo.get_cursor(target)
        assert cur is not None
        assert cur["source"] == "blockfrost_history"
        assert cur["cursor"] == "page:5;from:100"
        assert int(cur["txs_seen"]) == 3 and not cur["done"]
    finally:
        repo.close()
