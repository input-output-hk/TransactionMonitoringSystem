"""Unit tests for the analysis engine orchestrator."""

import pytest
from unittest.mock import patch, MagicMock
from app.analysis.engine import _score_transaction, _build_scorers, _CLASS_NAMES


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
        assert result["risk_band"] == "Low"

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
        for field in ("tx_hash", "network", "max_score", "max_class",
                       "risk_band", "sub_scores", "analysis_version", "analyzed_at"):
            assert field in result


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
