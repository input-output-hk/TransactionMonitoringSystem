"""Unit tests for baseline recomputation scheduling."""

import pytest
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
