"""Pins the performance tier's namespace-isolation invariants.

The synthetic "perftest" namespace stays invisible to operators because the
public network taxonomy rejects it at the API boundary, while the load
harness can still boot a server scoped to it because CARDANO_NETWORK is a
plain string. Both facts live in type annotations that a natural hygiene
change could silently flip, and neither the hermetic suite nor the gated
perf tier would notice (the tier never boots the app). These tests turn the
prose contract (backend/perf/README.md, docs/PERFORMANCE.md) into a failing
build. Hermetic on purpose: they must run on every `pytest tests/`.
"""

from typing import get_args

from app.config import Settings
from app.models.transaction import NetworkType
from perf import PERF_NETWORK


def test_perf_namespace_rejected_by_public_network_taxonomy():
    assert PERF_NETWORK not in get_args(NetworkType), (
        "adding the perf namespace to NetworkType would expose synthetic "
        "benchmark rows through every read endpoint of every deployment"
    )


def test_cardano_network_setting_admits_the_perf_namespace():
    # The documented load-harness workflow starts the server with
    # CARDANO_NETWORK=perftest; tightening this setting to NetworkType would
    # break that workflow at server startup, far from any test that runs on
    # this repo by default.
    assert Settings.model_fields["CARDANO_NETWORK"].annotation is str, (
        "CARDANO_NETWORK must stay a plain str so the perf load harness can "
        "scope a server to the perftest namespace"
    )
