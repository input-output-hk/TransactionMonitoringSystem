"""Unit tests for the transfer graph cycle detection module."""

import pytest
from unittest.mock import patch, MagicMock

from app.analysis.graph import detect_cycle, _build_cycle_result


class TestBuildCycleResult:
    def test_basic_cycle_metrics(self):
        result = _build_cycle_result(
            cycle_length=3,
            addresses=["addr_a", "addr_b", "addr_c", "addr_a"],
            origin_amount=10_000_000,
            final_amount=9_500_000,
            hops=[
                {"address": "addr_a", "amount_lovelace": 10_000_000, "slot": 100},
                {"address": "addr_b", "amount_lovelace": 9_800_000, "slot": 105},
                {"address": "addr_a", "amount_lovelace": 9_500_000, "slot": 110},
            ],
            origin_addresses={"addr_a"},
        )
        assert result["cycle_length"] == 3
        assert 0.9 < result["amount_similarity"] <= 1.0
        assert 0 < result["net_loss_ratio"] < 0.1
        assert result["origin_cluster"] == "addr_a"

    def test_round_amount_flag(self):
        result = _build_cycle_result(
            cycle_length=2,
            addresses=["a", "b", "a"],
            origin_amount=5_000_000,  # 5 ADA, round
            final_amount=4_800_000,
            hops=[
                {"address": "a", "amount_lovelace": 5_000_000, "slot": 10},
                {"address": "a", "amount_lovelace": 4_800_000, "slot": 15},
            ],
            origin_addresses={"a"},
        )
        assert result["round_amount_flag"] is True

    def test_non_round_amount(self):
        result = _build_cycle_result(
            cycle_length=2,
            addresses=["a", "b", "a"],
            origin_amount=5_123_456,
            final_amount=5_000_000,
            hops=[
                {"address": "a", "amount_lovelace": 5_123_456, "slot": 10},
                {"address": "a", "amount_lovelace": 5_000_000, "slot": 15},
            ],
            origin_addresses={"a"},
        )
        assert result["round_amount_flag"] is False

    def test_zero_origin_amount(self):
        result = _build_cycle_result(
            cycle_length=2,
            addresses=["a", "b"],
            origin_amount=0,
            final_amount=0,
            hops=[
                {"address": "a", "amount_lovelace": 0, "slot": 10},
                {"address": "b", "amount_lovelace": 0, "slot": 15},
            ],
            origin_addresses={"a"},
        )
        assert result["amount_similarity"] == 0.0
        assert result["net_loss_ratio"] == 1.0


class TestDetectCycle:
    @patch("app.analysis.graph.clickhouse")
    def test_no_inputs_returns_none(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        client.execute.return_value = []  # no input addresses
        assert detect_cycle("tx1", "preprod") is None

    @patch("app.analysis.graph.clickhouse")
    def test_no_outputs_returns_none(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        # First call: input addresses
        # Second call: output addresses (empty)
        client.execute.side_effect = [
            [("addr_origin",)],
            [],
        ]
        assert detect_cycle("tx1", "preprod") is None

    @patch("app.analysis.graph.clickhouse")
    def test_too_many_recipients_returns_none(self, mock_ch):
        client = MagicMock()
        mock_ch._get_client.return_value = client
        client.execute.side_effect = [
            [("addr_origin",)],
            [(f"addr_{i}", 1_000_000, 100) for i in range(25)],  # 25 recipients
        ]
        assert detect_cycle("tx1", "preprod") is None
