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
        # No prior baselines: first-ever rows always pass the drift guard.
        mock_ch.get_baseline.return_value = None
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


class TestDriftGuard:
    """Drift guard (baselines.drift): a recompute whose p99 jumps beyond the
    threshold is HELD (prior baseline stays active) and recorded, closing the
    baseline-poisoning path where one wide-distribution dump de-sensitises a
    per-script scorer."""

    def _row(self, p99, feature="value_cbor_bytes", p50=5.0):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return ("preprod", "per_script", "addrA", feature, p50, p99, 300, now, 90)

    @patch("app.analysis.baselines.clickhouse")
    def test_drift_holds_previous_baseline(self, mock_ch):
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 5.0, "p99": 10.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        kept = _filter_drifted([self._row(p99=100.0)])  # 9x jump
        assert kept == []
        mock_ch.insert_baseline_drift_event.assert_called_once()
        args = mock_ch.insert_baseline_drift_event.call_args.args
        assert args[4] == 10.0   # old_p99
        assert args[5] == 100.0  # new_p99

    @patch("app.analysis.baselines.clickhouse")
    def test_small_change_inserts_normally(self, mock_ch):
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 5.0, "p99": 10.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        rows = [self._row(p99=12.0)]  # 20% < 50% threshold
        assert _filter_drifted(rows) == rows
        mock_ch.insert_baseline_drift_event.assert_not_called()

    @patch("app.analysis.baselines.clickhouse")
    def test_first_baseline_always_passes(self, mock_ch):
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = None
        rows = [self._row(p99=1_000_000.0)]
        assert _filter_drifted(rows) == rows
        mock_ch.insert_baseline_drift_event.assert_not_called()

    @patch("app.analysis.baselines.clickhouse")
    def test_narrowing_p99_recompute_applies_and_records(self, mock_ch):
        # The poisoned-first-baseline recovery path: a wide stored p99
        # narrowing back down is strictly MORE sensitive (recall-safe), so
        # it must apply — holding it made the poisoned row self-protecting.
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 5.0, "p99": 100.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        rows = [self._row(p99=10.0)]  # 90% change, narrowing
        assert _filter_drifted(rows) == rows
        kwargs = mock_ch.insert_baseline_drift_event.call_args.kwargs
        assert kwargs["axis"] == "p99"
        assert kwargs["applied"] is True

    @patch("app.analysis.baselines.clickhouse")
    def test_zero_p99_prior_never_holds(self, mock_ch):
        # A p99=0 prior is rejected as unusable at resolution time, so it
        # protects nothing; its first positive recompute must apply.
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 0.0, "p99": 0.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        rows = [self._row(p99=5.0, p50=0.0)]
        assert _filter_drifted(rows) == rows

    @patch("app.analysis.baselines.clickhouse")
    def test_rising_p50_recompute_held(self, mock_ch):
        # Median poisoning: normalise() subtracts p50 first, so raising it
        # de-sensitises the axis exactly like widening p99.
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 5.0, "p99": 10.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        kept = _filter_drifted([self._row(p99=11.0, p50=20.0)])
        assert kept == []
        kwargs = mock_ch.insert_baseline_drift_event.call_args.kwargs
        assert kwargs["axis"] == "p50"
        assert kwargs["applied"] is False

    @patch("app.analysis.baselines.clickhouse")
    def test_falling_p50_recompute_applies(self, mock_ch):
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 5.0, "p99": 10.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        rows = [self._row(p99=10.0, p50=1.0)]
        assert _filter_drifted(rows) == rows

    @patch("app.analysis.baselines.clickhouse")
    def test_rising_p50_from_zero_held(self, mock_ch):
        # Unlike p99=0 (unusable baseline), a p50=0 prior is fully usable
        # and maximally sensitive; holding the rise keeps it that way.
        from app.analysis.baselines import _filter_drifted
        mock_ch.get_baseline.return_value = {
            "p50": 0.0, "p99": 10.0, "sample_count": 300,
            "computed_at": None, "window_days": 90,
        }
        kept = _filter_drifted([self._row(p99=10.0, p50=2.0)])
        assert kept == []


class TestChainTimeWindows:
    """Baseline percentile queries window on chain time (transactions.timestamp)
    via a JOIN, and use deterministic exact quantiles."""

    @patch("app.db.clickhouse._get_client")
    def test_percentile_query_uses_chain_time_and_exact(self, mock_get_client):
        from app.analysis.baselines import _query_percentiles
        client = MagicMock()
        mock_get_client.return_value = client
        client.execute.return_value = [(10.0, 99.0, 500)]
        result = _query_percentiles("utxo_features", "value_cbor_bytes", "preprod", 180)
        assert result == (10.0, 99.0, 500)
        sql = client.execute.call_args.args[0]
        assert "quantileExact" in sql
        assert "FROM transactions FINAL" in sql  # chain-time join, deduped
        assert "t.timestamp >=" in sql
        assert "ingestion_timestamp" not in sql

    @patch("app.db.clickhouse._get_client")
    def test_active_scripts_window_parametrized_from_config(self, mock_get_client):
        # The per-script window is a config knob (baselines.windows), not a
        # bare SQL literal; tuning it in detection.yaml must reach the query.
        from app.analysis import baselines
        client = MagicMock()
        mock_get_client.return_value = client
        client.execute.return_value = []
        baselines.get_active_script_addresses("preprod")
        sql = client.execute.call_args.args[0]
        params = client.execute.call_args.args[1]
        assert "INTERVAL %(days)s DAY" in sql
        assert "INTERVAL 90" not in sql
        assert params["days"] == baselines._PER_SCRIPT_WINDOW_DAYS

    @patch("app.analysis.baselines._query_percentiles")
    @patch("app.analysis.baselines.clickhouse")
    def test_global_window_days_from_config(self, mock_ch, mock_qp):
        from app.analysis import baselines
        mock_ch.get_baseline.return_value = None
        mock_qp.return_value = (1.0, 9.0, 500)
        rows = baselines.compute_global_baselines("preprod")
        assert rows
        # Window argument and the persisted window_days column both come
        # from the config, so the YAML is the single source of truth.
        assert mock_qp.call_args.args[3] == baselines._GLOBAL_WINDOW_DAYS
        assert rows[0][8] == baselines._GLOBAL_WINDOW_DAYS
