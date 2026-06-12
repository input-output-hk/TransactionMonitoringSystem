"""Unit tests for baseline recomputation scheduling."""

from unittest.mock import patch, MagicMock

from app.analysis.baselines import (
    check_drift,
    compute_global_baselines,
    get_active_script_addresses,
)


class TestCheckDrift:
    def test_no_drift(self):
        assert check_drift(100.0, 110.0, threshold=0.50) is False

    def test_drift_detected(self):
        assert check_drift(100.0, 200.0, threshold=0.50) is True

    def test_zero_old_p99(self):
        assert check_drift(0.0, 5.0) is True

    def test_zero_both(self):
        assert check_drift(0.0, 0.0) is False

    def test_exact_threshold(self):
        # 50% drift exactly at 0.50 threshold: abs(150 - 100) / 100 = 0.50
        # > 0.50 is False because it's not strictly greater
        assert check_drift(100.0, 150.0, threshold=0.50) is False


class TestComputeGlobalBaselines:
    @patch("app.analysis.baselines.clickhouse")
    def test_no_data_returns_empty(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        # All percentile queries return no data
        client.execute.return_value = []
        rows = compute_global_baselines("preprod")
        assert rows == []


class TestGetActiveScriptAddresses:
    @patch("app.analysis.baselines.clickhouse")
    def test_returns_addresses(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        client.execute.return_value = [
            ("addr_script_1", 500),
            ("addr_script_2", 300),
        ]
        result = get_active_script_addresses("preprod", limit=10)
        assert result == ["addr_script_1", "addr_script_2"]

    @patch("app.analysis.baselines.clickhouse")
    def test_handles_error(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        client.execute.side_effect = Exception("connection failed")
        result = get_active_script_addresses("preprod")
        assert result == []


class TestMultipleSatPerScriptBaselines:
    """multiple_sat extraction baselines are emitted per_script ONLY."""

    @patch("app.analysis.baselines.clickhouse")
    def test_emits_per_script_only_for_all_features(self, mock_ch):
        from app.analysis.baselines import (
            compute_multiple_sat_per_script_baselines,
            _MULTIPLE_SAT_PER_SCRIPT_FEATURES,
        )
        mock_ch.query_multiple_sat_extraction_percentiles.return_value = [
            {
                "script": "addrA", "sample_count": 300,
                "net_value_out_of_script": (5_000_000.0, 50_000_000.0),
                "n_assets_out_of_script": (2.0, 4.0),
                "exunits_per_script_input": (1_000_000.0, 2_000_000.0),
                "n_inputs_same_script": (2.0, 3.0),
            },
        ]
        rows = compute_multiple_sat_per_script_baselines("preprod")

        # One row per feature, all per_script, all for addrA.
        assert len(rows) == len(_MULTIPLE_SAT_PER_SCRIPT_FEATURES)
        assert {r[3] for r in rows} == set(_MULTIPLE_SAT_PER_SCRIPT_FEATURES)
        assert all(r[1] == "per_script" for r in rows)   # scope_type
        assert all(r[2] == "addrA" for r in rows)        # scope_id
        assert all(r[6] == 300 for r in rows)            # sample_count
        # Never global — the whole point.
        assert not any(r[1] == "global" for r in rows)
        mock_ch.insert_baselines.assert_called_once()

    @patch("app.analysis.baselines.clickhouse")
    def test_no_qualifying_scripts_writes_nothing(self, mock_ch):
        from app.analysis.baselines import compute_multiple_sat_per_script_baselines
        mock_ch.query_multiple_sat_extraction_percentiles.return_value = []
        rows = compute_multiple_sat_per_script_baselines("preprod")
        assert rows == []
        mock_ch.insert_baselines.assert_not_called()


class TestExtractionPercentilesReshape:
    """The evidence-column -> feature reshape must preserve order."""

    @patch("app.db.clickhouse._get_client")
    def test_columns_map_to_features_in_order(self, mock_get_client):
        from app.db.clickhouse import (
            query_multiple_sat_extraction_percentiles,
            _MULTIPLE_SAT_EVIDENCE_KEYS,
        )
        client = MagicMock()
        mock_get_client.return_value = client
        # script, cnt, then (p50, p99) per feature in _MULTIPLE_SAT_EVIDENCE_KEYS order.
        client.execute.return_value = [
            ("addrA", 300, 5_000_000.0, 50_000_000.0, 2.0, 4.0),
        ]
        recs = query_multiple_sat_extraction_percentiles("preprod", 90, 200)
        assert len(recs) == 1
        rec = recs[0]
        assert rec["script"] == "addrA"
        assert rec["sample_count"] == 300
        # Only the value axis is calibrated per-script.
        assert {f for f, _k in _MULTIPLE_SAT_EVIDENCE_KEYS} == {
            "net_value_out_of_script", "n_assets_out_of_script"}
        # First feature in the allowlist gets the first (p50, p99) pair.
        first_feature = _MULTIPLE_SAT_EVIDENCE_KEYS[0][0]
        assert rec[first_feature] == (5_000_000.0, 50_000_000.0)
        assert rec["n_assets_out_of_script"] == (2.0, 4.0)
