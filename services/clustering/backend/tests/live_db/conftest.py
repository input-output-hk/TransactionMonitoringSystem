"""Opt-in live-database test tier for the clustering sidecar.

Mirrors the host's ``backend/tests/live_db`` tier and shares its opt-in flag:
the hermetic suite pins every ClickHouse call against fakes, which is exactly
how server-side breakage ships with green tests (aggregate-alias shadowing and
projection-gated DDL both did, on ClickHouse 26.x). This tier runs the
module's real query text — the hybrid union reads, the history boundary
aggregates, the host-membership publish bound — against a live server so
dialect- and type-level errors surface before a deploy.

Opt in with ``TMS_LIVE_DB_TESTS=1``; without it the whole directory is skipped
at collection and ``pytest tests/`` stays hermetic. Connection settings come
from the normal ``CLICKHOUSE_*`` environment (the repo docker-compose defaults
apply otherwise), and both databases must exist with their schemas applied —
``tms_clustering`` (the module's init SQL) and ``tms_analytics`` (the host's).

Everything these tests write is scoped to a throwaway UUID target and the
``livedbtest`` network namespace, so pointing them at a dev database does not
pollute operator-visible data (every production read path is target- or
network-scoped).
"""

import os

_LIVE_DB_ENV = "TMS_LIVE_DB_TESTS"

# Namespace for the host-side (network-scoped) reads; nothing is ever written
# to the host tables, but the synthetic network keeps even the reads disjoint
# from real data.
LIVE_NETWORK = "livedbtest"

if not os.environ.get(_LIVE_DB_ENV):
    collect_ignore_glob = ["test_*.py"]
