"""Unit tests for the analysis engine orchestrator."""

from unittest.mock import patch

from app.analysis.engine import (
    _CLASS_NAMES,
    _analysis_defer_attempts,
    _build_scorers,
    _handle_incomplete_scoring,
    _score_transaction,
)
from app.analysis.normalise import score_to_band
from app.analysis.scorers.base import ScorerResult


class _FixedScorer:
    """Test double that gates open and returns a fixed score for a class."""

    def __init__(self, name, score):
        self.name = name
        self._score = score

    def gate(self, features):
        return True

    def score(self, features):
        return ScorerResult(score=self._score)


def _make_row(tx_hash="tx01", metadata=None, raw_data=None):
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "fee": 200_000,
        "input_count": 2,
        "output_count": 3,
        "total_output_value": 10_000_000,
        "metadata": metadata,
        "addresses": ["addr_test1qzabc"],
        "raw_data": raw_data or "{}",
        "slot": 50000,
        "block_height": 1000,
        "timestamp": "2025-01-01T00:00:00Z",
    }


class TestScoreTransaction:
    def test_all_classes_present(self):
        """Result dict should have all 9 class keys."""
        row = _make_row()
        scorers = _build_scorers()
        result = _score_transaction(row, scorers)
        for name in _CLASS_NAMES:
            assert name in result
            # No gates pass on a plain tx, so all should be -1
            assert result[name] == -1.0

    def test_max_score_zero_when_no_gates_pass(self):
        row = _make_row()
        result = _score_transaction(row, _build_scorers())
        assert result["max_score"] == 0.0
        assert result["max_class"] == ""
        assert result["risk_band"] == "Informational"

    def test_metadata_dict_parsed(self):
        """Dict metadata should be usable by scorers."""
        row = _make_row(metadata={"674": "visit https://cardano-airdrop.scam.com"})
        result = _score_transaction(row, _build_scorers())
        # Phishing gate should pass
        assert result["phishing"] >= 0

    def test_scorer_exception_handled(self):
        """A crashing scorer should not break the pipeline."""

        class BadScorer:
            name = "phishing"

            def gate(self, features):
                raise RuntimeError("boom")

        row = _make_row()
        result = _score_transaction(row, [BadScorer()])
        assert result["phishing"] == -1.0  # stays at default

    def test_result_has_required_fields(self):
        row = _make_row()
        result = _score_transaction(row, _build_scorers())
        for field in (
            "tx_hash",
            "network",
            "max_score",
            "max_class",
            "risk_band",
            "sub_scores",
            "analysis_version",
            "analyzed_at",
        ):
            assert field in result


class TestCorroboration:
    def test_counts_only_classes_above_threshold(self):
        """corroboration_count = number of distinct classes at/above the
        threshold; corroborating_classes lists them sorted. A sub-threshold
        class is excluded."""
        row = _make_row()
        scorers = [
            _FixedScorer("sandwich", 50.0),  # >= 40 -> counts
            _FixedScorer("token_dust", 45.0),  # >= 40 -> counts
            _FixedScorer("circular", 10.0),  # < 40 -> excluded
        ]
        result = _score_transaction(row, scorers)
        assert result["corroboration_count"] == 2
        assert result["corroborating_classes"] == "sandwich,token_dust"

    def test_single_class_not_corroborated(self):
        row = _make_row()
        result = _score_transaction(row, [_FixedScorer("sandwich", 90.0)])
        assert result["corroboration_count"] == 1
        assert result["corroborating_classes"] == "sandwich"

    def test_corroboration_does_not_change_band(self):
        """Two corroborating classes both in the Moderate range must NOT
        escalate the band: risk_band stays exactly score_to_band(max_score).
        This pins Option B (flag-only) against any future regression that wires
        corroboration into banding."""
        row = _make_row()
        scorers = [_FixedScorer("sandwich", 50.0), _FixedScorer("token_dust", 45.0)]
        result = _score_transaction(row, scorers)
        assert result["max_score"] == 50.0
        assert result["risk_band"] == score_to_band(result["max_score"]) == "Moderate"


class TestBuildScorers:
    def test_all_scorers_returned(self):
        scorers = _build_scorers()
        names = {s.name for s in scorers}
        assert names == set(_CLASS_NAMES)

    @patch("app.analysis.engine.settings")
    def test_disabled_scorer_excluded(self, mock_settings):
        mock_settings.ANALYSIS_ENABLED = True
        # Disable phishing, enable everything else
        for name in _CLASS_NAMES:
            flag = f"SCORER_{name.upper()}_ENABLED"
            setattr(mock_settings, flag, name != "phishing")
        scorers = _build_scorers()
        names = {s.name for s in scorers}
        assert "phishing" not in names
        assert len(names) == 8


class _RaisingScorer:
    """Test double whose gate opens but whose score() raises, simulating a
    crafted-input crash or a transient dependency error inside one scorer."""

    def __init__(self, name):
        self.name = name

    def gate(self, features):
        return True

    def score(self, features):
        raise RuntimeError("boom")


def _incomplete_result(tx_hash="txfail", failed_scorers=None, failed_enrichment=None):
    return {
        "tx_hash": tx_hash,
        "network": "preprod",
        "evidence": {},
        "_failed_scorers": failed_scorers or [],
        "_enrichment_failed": failed_enrichment or [],
    }


class TestIncompleteScoring:
    """Recall-first: a scorer that raises, or an enrichment that fails, must
    not silently persist a row with the affected class at the -1 sentinel
    (which the unanalyzed anti-join would then treat as permanently scored).
    The tx is deferred and retried, then degraded with a visible marker."""

    def setup_method(self):
        _analysis_defer_attempts.clear()

    def teardown_method(self):
        _analysis_defer_attempts.clear()

    def test_raising_scorer_is_reported_not_swallowed(self):
        row = _make_row()
        result = _score_transaction(row, [_RaisingScorer("front_running")])
        # The class stays at the -1 sentinel (it could not be scored)...
        assert result["front_running"] == -1.0
        # ...but the failure is surfaced so the engine can defer, not swallow.
        assert result["_failed_scorers"] == ["front_running"]

    @patch("app.analysis.engine.settings")
    def test_incomplete_tx_is_deferred_then_degraded(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = True
        ms.ANALYSIS_DEFER_MAX_ATTEMPTS = 2
        ms.ANALYSIS_DEFER_RETRY_SECONDS = 0  # never pace-skip counting in test
        # Attempt 1 (1 < 2): deferred, dropped from the batch, no row written.
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_scorers=["phishing"])], "preprod"
        )
        assert kept == []
        # Attempt 2 (2 < 2 is False): budget exhausted, kept WITH a marker.
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_scorers=["phishing"])], "preprod"
        )
        assert len(kept) == 1
        assert kept[0]["evidence"]["_meta"]["scorer_failed"] == ["phishing"]

    @patch("app.analysis.engine.settings")
    def test_enrichment_failure_deferred_like_scorer_failure(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = True
        ms.ANALYSIS_DEFER_MAX_ATTEMPTS = 1  # degrade immediately
        ms.ANALYSIS_DEFER_RETRY_SECONDS = 0
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_enrichment=["collision"])], "preprod"
        )
        assert len(kept) == 1
        assert kept[0]["evidence"]["_meta"]["enrichment_unavailable"] == ["collision"]

    @patch("app.analysis.engine.settings")
    def test_complete_tx_passes_through_and_clears_ledger(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = True
        ms.ANALYSIS_DEFER_MAX_ATTEMPTS = 3
        ms.ANALYSIS_DEFER_RETRY_SECONDS = 0
        _analysis_defer_attempts[("preprod", "txok")] = (1, 0.0)  # stale entry
        kept = _handle_incomplete_scoring([_incomplete_result(tx_hash="txok")], "preprod")
        assert len(kept) == 1
        assert ("preprod", "txok") not in _analysis_defer_attempts
        # Transient bookkeeping keys are stripped before insert.
        assert "_failed_scorers" not in kept[0]
        assert "_enrichment_failed" not in kept[0]

    @patch("app.analysis.engine.settings")
    def test_defer_disabled_keeps_row_but_strips_transient_keys(self, ms):
        ms.ANALYSIS_DEFER_ENABLED = False
        kept = _handle_incomplete_scoring(
            [_incomplete_result(failed_scorers=["phishing"])], "preprod"
        )
        assert len(kept) == 1
        assert "_failed_scorers" not in kept[0]
        assert "_enrichment_failed" not in kept[0]


from datetime import UTC, datetime

import app.analysis.engine as _engine_mod


class TestRescanBoundAndFallback:
    """The periodic full rescan must be cost-bounded and must never block the
    cheap watermark path when it fails (which would halt all scoring)."""

    def setup_method(self):
        _engine_mod._last_full_rescan.clear()
        _engine_mod._unanalyzed_watermark.clear()

    def teardown_method(self):
        _engine_mod._last_full_rescan.clear()
        _engine_mod._unanalyzed_watermark.clear()

    @patch("app.analysis.engine.settings")
    def test_poll_since_bounds_rescan_window(self, ms):
        ms.UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS = 600
        ms.UNANALYZED_FULL_RESCAN_WINDOW_SECONDS = 604800  # 7 days
        since, full = _engine_mod._poll_since("preprod")
        assert full is True
        assert since is not None  # bounded lookback, not the whole table
        assert isinstance(since, datetime)

    @patch("app.analysis.engine.settings")
    def test_poll_since_unbounded_when_window_zero(self, ms):
        ms.UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS = 600
        ms.UNANALYZED_FULL_RESCAN_WINDOW_SECONDS = 0  # legacy unbounded
        since, full = _engine_mod._poll_since("preprod")
        assert full is True
        assert since is None

    @patch("app.analysis.engine.clickhouse")
    @patch("app.analysis.engine.settings")
    def test_rescan_failure_falls_back_to_watermark(self, ms, mock_ch):
        ms.ANALYSIS_ENABLED = True
        ms.ANALYSIS_ENGINE_BATCH_SIZE = 100
        ms.UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS = 600
        ms.UNANALYZED_FULL_RESCAN_WINDOW_SECONDS = 0  # since=None rescan
        wm = datetime(2025, 1, 1, tzinfo=UTC)
        _engine_mod._unanalyzed_watermark["preprod"] = wm
        mock_ch.get_unanalyzed_transactions.side_effect = [RuntimeError("CH OOM"), []]
        result = _engine_mod.run_once("preprod")
        assert result == 0
        assert mock_ch.get_unanalyzed_transactions.call_count == 2
        assert mock_ch.get_unanalyzed_transactions.call_args_list[1].kwargs["since"] == wm
        assert "preprod" in _engine_mod._last_full_rescan

    @patch("app.analysis.engine.clickhouse")
    @patch("app.analysis.engine.settings")
    def test_rescan_failure_reraises_without_watermark(self, ms, mock_ch):
        ms.ANALYSIS_ENABLED = True
        ms.ANALYSIS_ENGINE_BATCH_SIZE = 100
        ms.UNANALYZED_FULL_RESCAN_INTERVAL_SECONDS = 600
        ms.UNANALYZED_FULL_RESCAN_WINDOW_SECONDS = 0
        mock_ch.get_unanalyzed_transactions.side_effect = RuntimeError("CH OOM")
        import pytest as _pytest

        with _pytest.raises(RuntimeError):
            _engine_mod.run_once("preprod")


class _EvidenceScorer:
    """Test double that gates open and returns a fixed score plus evidence."""

    def __init__(self, name, score, evidence):
        self.name = name
        self._score = score
        self._evidence = evidence

    def gate(self, features):
        return True

    def score(self, features):
        return ScorerResult(score=self._score, evidence=self._evidence)


class TestContractAddressOnScoreRow:
    """The engine normalizes the winning class's contract into its own column.

    Grouping alerts by contract reads this column, so it has to be written at
    scoring time from whichever evidence key the winning scorer used.
    """

    SCRIPT_ADDR = "addr_test1wq9contracttarget"

    def test_winning_class_target_is_persisted(self):
        row = _make_row()
        scorer = _EvidenceScorer(
            "large_datum",
            70.0,
            {"target_script_address": self.SCRIPT_ADDR},
        )
        result = _score_transaction(row, [scorer])
        assert result["max_class"] == "large_datum"
        assert result["contract_address"] == self.SCRIPT_ADDR

    def test_losing_class_target_is_not_attributed_to_the_winner(self):
        row = _make_row()
        winner = _EvidenceScorer("token_dust", 80.0, {"target_script_address": self.SCRIPT_ADDR})
        loser = _EvidenceScorer(
            "large_datum",
            20.0,
            {"target_script_address": "addr_test1wq9OTHER"},
        )
        result = _score_transaction(row, [winner, loser])
        assert result["max_class"] == "token_dust"
        assert result["contract_address"] == self.SCRIPT_ADDR

    def test_class_without_contract_identity_writes_the_sentinel(self):
        row = _make_row()
        scorer = _EvidenceScorer("phishing", 75.0, {"urls": ["http://evil.example"]})
        result = _score_transaction(row, [scorer])
        assert result["max_class"] == "phishing"
        assert result["contract_address"] == ""

    def test_no_applicable_class_writes_the_sentinel(self):
        result = _score_transaction(_make_row(), _build_scorers())
        assert result["max_class"] == ""
        assert result["contract_address"] == ""


class TestCycleEnrichment:
    """How a cycle read's outcome reaches the engine's defer-and-retry path."""

    def _enrich(self, detect):
        from app.analysis import enrichment

        row = _make_row()
        with (
            patch.object(enrichment.settings, "SCORER_CIRCULAR_ENABLED", True),
            patch.object(enrichment.settings, "CYCLE_DETECTION_ENABLED", True),
            patch("app.analysis.graph.detect_cycle", side_effect=detect),
        ):
            enrichment.enrich_cycle_features([row], "preprod")
        return row

    def test_a_failed_search_asks_for_a_retry_without_a_cycle(self):
        def detect(tx_hash, network):
            raise ConnectionError("ClickHouse went away")

        row = self._enrich(detect)
        assert "cycle" not in row
        assert row["_enrichment_failed"] == ["cycle"]

    def test_a_cycle_scored_without_its_history_is_kept_and_retried(self):
        """Kept, so a read that keeps failing still writes the cycle's score
        once the retries run out, instead of no circular finding at all."""
        cycle = {"cycle_length": 3, "history_unavailable": True}
        row = self._enrich(lambda tx_hash, network: cycle)
        assert row["cycle"] is cycle
        assert row["_enrichment_failed"] == ["cycle_history"]

    def test_a_complete_cycle_asks_for_nothing(self):
        cycle = {"cycle_length": 3, "history_unavailable": False}
        row = self._enrich(lambda tx_hash, network: cycle)
        assert row["cycle"] is cycle
        assert "_enrichment_failed" not in row
