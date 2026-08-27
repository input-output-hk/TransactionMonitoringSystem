"""Baseline provenance: what each axis was actually normalised against.

`baselines` is a ReplacingMergeTree maintained in place, so a recompute
replaces the row and the percentiles a past score was measured against become
unrecoverable. The caps in `resolved_or_bootstrap` widen the gap further: the
values a score used are deliberately NOT the values the table held. Recording
the pair at scoring time is the only way a stored verdict stays re-derivable,
which is what these tests pin.
"""

from unittest.mock import patch

import pytest

from app.analysis import scorer_config as sc
from app.analysis.engine import _score_transaction
from app.analysis.scorers.base import ScorerResult

FEATURE = "value_cbor_bytes"
SCOPE_ID = "script-abc"


@pytest.fixture
def anchors():
    return sc.get("large_value")["bootstrap_anchors"]


def _resolve(mocked, anchors, **kwargs):
    """Resolve one axis with `resolve_baseline` stubbed, and return the record.

    Patches the name inside `scorer_config`, not in `normalise`: the loader
    imports it, so patching the origin module leaves the bound reference alone
    and the real (DB-less, therefore "missing") path runs instead.
    """
    with patch("app.analysis.scorer_config.resolve_baseline", return_value=mocked):
        with sc.record_baselines() as records:
            used = sc.resolved_or_bootstrap(
                feature=FEATURE,
                scope_type="per_script",
                scope_id=SCOPE_ID,
                network="preprod",
                bootstrap=anchors,
                bootstrap_key=FEATURE,
                **kwargs,
            )
    return used, records


class TestRecording:
    def test_learned_baseline_is_recorded(self, anchors):
        (p50, p99, source), records = _resolve((200.0, 900.0, "per_script"), anchors)
        assert records == [
            {
                "feature": FEATURE,
                "scope": f"per_script:{SCOPE_ID}",
                "source": "per_script",
                "p50": p50,
                "p99": p99,
            }
        ]

    def test_uncapped_pair_is_kept_when_a_cap_bites(self, anchors):
        """The case the whole feature exists for.

        A poisoned per-script distribution is capped down before use, so the
        table's value and the value the score was measured against are two
        different numbers and neither is recoverable later. Both are recorded.
        """
        poisoned_p99 = 99_999.0
        (p50, p99, _), records = _resolve((100.0, poisoned_p99, "per_script"), anchors)
        assert p99 < poisoned_p99, "expected the p99 cap to bite for this fixture"
        assert records[0]["p99"] == p99
        assert records[0]["uncapped"] == {"p50": 100.0, "p99": poisoned_p99}

    def test_no_uncapped_key_when_nothing_was_capped(self, anchors):
        _, records = _resolve((200.0, 900.0, "per_script"), anchors)
        assert "uncapped" not in records[0]

    def test_bootstrap_fallback_records_the_anchor(self, anchors):
        """A miss has nothing to cap, so it reports the anchor and no pre-cap
        pair: an `uncapped` key here would imply a learned baseline existed."""
        (p50, p99, source), records = _resolve((0.0, 0.0, "missing"), anchors)
        assert source == "bootstrap"
        assert (records[0]["p50"], records[0]["p99"]) == (p50, p99)
        assert "uncapped" not in records[0]

    def test_nothing_collected_outside_a_recording_block(self, anchors):
        """Scoring paths that do not opt in must not pay for, or trip over,
        the recorder."""
        with patch(
            "app.analysis.scorer_config.resolve_baseline",
            return_value=(200.0, 900.0, "per_script"),
        ):
            sc.resolved_or_bootstrap(
                feature=FEATURE,
                scope_type="per_script",
                scope_id=SCOPE_ID,
                network="preprod",
                bootstrap=anchors,
                bootstrap_key=FEATURE,
            )
        assert sc._BASELINE_RECORD.get() is None

    def test_blocks_do_not_leak_into_each_other(self, anchors):
        _, first = _resolve((200.0, 900.0, "per_script"), anchors)
        _, second = _resolve((300.0, 950.0, "per_script"), anchors)
        assert len(first) == 1 and len(second) == 1
        assert first[0]["p50"] != second[0]["p50"]

    def test_switch_off_records_nothing(self, anchors, monkeypatch):
        """The kill switch matters because this writes to every scored row on
        the highest-traffic table."""
        monkeypatch.setattr(sc, "_RECORD_PROVENANCE", False)
        _, records = _resolve((200.0, 900.0, "per_script"), anchors)
        assert records == []


class _BaselineScorer:
    """Gates open and resolves one baseline, the way a real scorer does."""

    name = "large_value"

    def __init__(self, evidence=None):
        self._evidence = evidence or {}

    def gate(self, features):
        return True

    def score(self, features):
        sc.resolved_or_bootstrap(
            feature=FEATURE,
            scope_type="per_script",
            scope_id=SCOPE_ID,
            network="preprod",
            bootstrap=sc.get("large_value")["bootstrap_anchors"],
            bootstrap_key=FEATURE,
        )
        # Deliberately NOT a copy: a scorer is free to hand back its own dict,
        # so returning it directly is what lets test_scorer_evidence_is_not_mutated
        # exercise the engine's copy rather than this fixture's.
        return ScorerResult(score=50.0, evidence=self._evidence)


def _row():
    return {
        "tx_hash": "tx-provenance",
        "network": "preprod",
        "fee": 200_000,
        "input_count": 2,
        "output_count": 3,
        "total_output_value": 10_000_000,
        "metadata": None,
        "addresses": ["addr_test1qzabc"],
        "raw_data": "{}",
        "slot": 50000,
        "block_height": 1000,
        "timestamp": "2025-01-01T00:00:00Z",
    }


class TestEngineAttachesRecords:
    def test_records_land_in_the_scoring_class_evidence(self):
        scorer = _BaselineScorer(evidence={"script_address": "addr_test1w"})
        result = _score_transaction(_row(), [scorer])
        class_evidence = result["evidence"]["large_value"]
        assert class_evidence["script_address"] == "addr_test1w"
        assert [r["feature"] for r in class_evidence[sc.BASELINE_EVIDENCE_KEY]] == [FEATURE]

    def test_scorer_evidence_is_not_mutated(self):
        """The ScorerResult belongs to the scorer; the engine adds to a copy."""
        original = {"script_address": "addr_test1w"}
        scorer = _BaselineScorer(evidence=original)
        _score_transaction(_row(), [scorer])
        assert sc.BASELINE_EVIDENCE_KEY not in original

    def test_class_with_no_evidence_still_records_its_baselines(self):
        """Otherwise the provenance of a scorer that reports no evidence of its
        own would be silently dropped."""
        result = _score_transaction(_row(), [_BaselineScorer()])
        assert sc.BASELINE_EVIDENCE_KEY in result["evidence"]["large_value"]

    def test_scorer_resolving_nothing_gets_no_key(self):
        class _Quiet(_BaselineScorer):
            def score(self, features):
                return ScorerResult(score=10.0, evidence={"a": 1})

        result = _score_transaction(_row(), [_Quiet()])
        assert sc.BASELINE_EVIDENCE_KEY not in result["evidence"]["large_value"]
