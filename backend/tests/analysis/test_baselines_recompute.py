"""Unit tests for baseline recomputation scheduling."""

from unittest.mock import patch, MagicMock

from app.analysis.baselines import (
    _DRIFT_P50_THRESHOLD,
    _DRIFT_P99_THRESHOLD,
    INVERTED_CONSUMER_FEATURES,
    check_drift,
    compute_global_baselines,
    get_active_script_addresses,
)

# Thresholds come from the validated config (baselines.drift), not bare
# literals: check_drift deliberately has no default, so the tunable cannot
# bypass the loader.
_THR = _DRIFT_P99_THRESHOLD


class TestCheckDrift:
    def test_no_drift(self):
        assert check_drift(100.0, 100.0 * (1 + _THR / 2), threshold=_THR) is False

    def test_drift_detected(self):
        assert check_drift(100.0, 100.0 * (1 + 2 * _THR), threshold=_THR) is True

    def test_zero_old_p99(self):
        assert check_drift(0.0, 5.0, threshold=_THR) is True

    def test_zero_both(self):
        assert check_drift(0.0, 0.0, threshold=_THR) is False

    def test_exact_threshold(self):
        # A relative change of exactly the threshold is NOT drift: the
        # comparison is strictly greater-than.
        assert check_drift(100.0, 100.0 * (1 + _THR), threshold=_THR) is False


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
                "script": "addrA",
                "sample_count": 300,
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
        assert all(r[1] == "per_script" for r in rows)  # scope_type
        assert all(r[2] == "addrA" for r in rows)  # scope_id
        assert all(r[6] == 300 for r in rows)  # sample_count
        # Never global: the whole point.
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
            "net_value_out_of_script",
            "n_assets_out_of_script",
        }
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
            "p50": 5.0,
            "p99": 10.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        kept = _filter_drifted([self._row(p99=100.0)])  # 9x jump
        assert kept == []
        mock_ch.insert_baseline_drift_event.assert_called_once()
        args = mock_ch.insert_baseline_drift_event.call_args.args
        assert args[4] == 10.0  # old_p99
        assert args[5] == 100.0  # new_p99

    @patch("app.analysis.baselines.clickhouse")
    def test_small_change_inserts_normally(self, mock_ch):
        from app.analysis.baselines import _filter_drifted

        mock_ch.get_baseline.return_value = {
            "p50": 5.0,
            "p99": 10.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
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
        # narrowing back down is strictly MORE sensitive for a
        # plain-normalise feature (recall-safe), so it must apply; holding
        # it made the poisoned row self-protecting. Uses datum_bytes, a
        # pure-normalise feature: inverted-consumer features hold both
        # directions (see TestDriftGuardInvertedConsumers).
        from app.analysis.baselines import _filter_drifted

        assert "datum_bytes" not in INVERTED_CONSUMER_FEATURES
        mock_ch.get_baseline.return_value = {
            "p50": 5.0,
            "p99": 100.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        rows = [self._row(p99=10.0, feature="datum_bytes")]  # 90% narrowing
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
            "p50": 0.0,
            "p99": 0.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        rows = [self._row(p99=5.0, p50=0.0)]
        assert _filter_drifted(rows) == rows

    @patch("app.analysis.baselines.clickhouse")
    def test_rising_p50_recompute_held(self, mock_ch):
        # Median poisoning: normalise() subtracts p50 first, so raising it
        # de-sensitises the axis exactly like widening p99.
        from app.analysis.baselines import _filter_drifted

        mock_ch.get_baseline.return_value = {
            "p50": 5.0,
            "p99": 10.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        kept = _filter_drifted([self._row(p99=11.0, p50=20.0)])
        assert kept == []
        kwargs = mock_ch.insert_baseline_drift_event.call_args.kwargs
        assert kwargs["axis"] == "p50"
        assert kwargs["applied"] is False

    @patch("app.analysis.baselines.clickhouse")
    def test_falling_p50_recompute_applies(self, mock_ch):
        # Pure-normalise feature: a falling median is strictly more
        # sensitive, so the recompute applies.
        from app.analysis.baselines import _filter_drifted

        mock_ch.get_baseline.return_value = {
            "p50": 5.0,
            "p99": 10.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        rows = [self._row(p99=10.0, p50=1.0, feature="datum_bytes")]
        assert _filter_drifted(rows) == rows

    @patch("app.analysis.baselines.clickhouse")
    def test_rising_p50_from_zero_held(self, mock_ch):
        # Unlike p99=0 (unusable baseline), a p50=0 prior is fully usable
        # and maximally sensitive; holding the rise keeps it that way.
        from app.analysis.baselines import _filter_drifted

        mock_ch.get_baseline.return_value = {
            "p50": 0.0,
            "p99": 10.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        kept = _filter_drifted([self._row(p99=10.0, p50=2.0)])
        assert kept == []


class TestDriftGuardInvertedConsumers:
    """ada_amount and value_cbor_bytes feed normalise_inverted() axes
    (token_dust / large_value s_ada, large_datum s_value_inv), where a
    DOWNWARD-poisoned baseline zeroes the dust signal. The drift guard must
    hold BOTH directions for these features: the direction-aware guard
    alone treated falling values as recall-safe, so one low-ADA dump
    recompute de-sensitised the inverted axes (review finding)."""

    def _row(self, feature, p50, p99):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return ("preprod", "per_script", "addrA", feature, p50, p99, 300, now, 90)

    @patch("app.analysis.baselines.clickhouse")
    def test_downward_p99_drift_on_inverted_feature_held(self, mock_ch):
        from app.analysis.baselines import _filter_drifted

        assert "value_cbor_bytes" in INVERTED_CONSUMER_FEATURES
        mock_ch.get_baseline.return_value = {
            "p50": 100.0,
            "p99": 1500.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        # p99 collapses 1500 -> 400 (ratio 0.73 > threshold), p50 stable.
        kept = _filter_drifted([self._row("value_cbor_bytes", p50=100.0, p99=400.0)])
        assert kept == []
        kwargs = mock_ch.insert_baseline_drift_event.call_args.kwargs
        assert kwargs["axis"] == "p99"
        assert kwargs["applied"] is False

    @patch("app.analysis.baselines.clickhouse")
    def test_downward_p50_drift_on_inverted_feature_held(self, mock_ch):
        from app.analysis.baselines import _filter_drifted

        assert "ada_amount" in INVERTED_CONSUMER_FEATURES
        mock_ch.get_baseline.return_value = {
            "p50": 1_200_000.0,
            "p99": 2_000_000.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        # p50 falls 1.2M -> 0.5M (ratio 0.58 > threshold), p99 stable.
        kept = _filter_drifted([self._row("ada_amount", p50=500_000.0, p99=1_900_000.0)])
        assert kept == []
        kwargs = mock_ch.insert_baseline_drift_event.call_args.kwargs
        assert kwargs["axis"] == "p50"
        assert kwargs["applied"] is False

    @patch("app.analysis.baselines.clickhouse")
    def test_downward_drift_on_pure_normalise_feature_applies(self, mock_ch):
        # The recall-safe direction still applies for plain-normalise
        # features; only inverted-consumer features get the symmetric hold.
        from app.analysis.baselines import _filter_drifted

        assert "datum_bytes" not in INVERTED_CONSUMER_FEATURES
        mock_ch.get_baseline.return_value = {
            "p50": 1_200_000.0,
            "p99": 2_000_000.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        rows = [self._row("datum_bytes", p50=500_000.0, p99=600_000.0)]
        assert _filter_drifted(rows) == rows

    @patch("app.analysis.baselines.clickhouse")
    def test_dust_signal_survives_downward_poisoning_recompute(self, mock_ch, monkeypatch):
        """ATTACK-MUST-FIRE: an attacker dumps low-ADA outputs at a victim
        script to drag the ada_amount percentiles down, then sends the real
        dust bundle. The poisoned recompute must be HELD, and with the
        surviving (honest) baseline the token_dust inverted-ADA axis must
        still saturate on the dust bundle."""
        import app.analysis.scorer_config as sc
        from app.analysis import normalise as norm
        from app.analysis.baselines import _filter_drifted
        from app.analysis.normalise import BAND_MODERATE_THRESHOLD, normalise_inverted
        from app.analysis.scorers.token_dust import TokenDustScorer

        # Honest per-script baseline at the token_dust bootstrap values
        # (min-UTxO dust economics), straight from the validated config.
        anchors = sc.get("token_dust")["bootstrap_anchors"]
        honest_p50, honest_p99 = sc.anchor(anchors, "ada_amount")
        honest = {
            "p50": honest_p50,
            "p99": honest_p99,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        # Attacker's downward recompute: percentiles dragged far below the
        # honest window by a low-ADA output flood.
        poisoned_p50, poisoned_p99 = honest_p50 / 100.0, honest_p99 / 40.0

        # 1. The poisoned recompute is HELD (prior row stays active).
        mock_ch.get_baseline.return_value = dict(honest)
        kept = _filter_drifted([self._row("ada_amount", p50=poisoned_p50, p99=poisoned_p99)])
        assert kept == []

        # 2. With the surviving baseline, the dust bundle still fires.
        def _resolver(network, scope_type, scope_id, feature):
            if (scope_type, feature) == ("per_script", "ada_amount"):
                return dict(honest)
            return None  # other axes fall back to bootstrap

        monkeypatch.setattr(norm.clickhouse, "get_baseline", _resolver)
        dust_value = {"lovelace": int(honest_p50)}  # min-UTxO dust ADA
        for i in range(20):
            dust_value[f"policy{i:03d}" + "0" * 50] = {"tok": 1}
        features = {
            "tx_hash": "dust_poison",
            "network": "preprod",
            "raw_data": {
                "outputs": [
                    {"address": "addr_test1wz5fxvalex", "value": dust_value},
                ]
            },
        }
        result = TokenDustScorer().score(features)
        assert result.sub_scores["lovelace_inverted"] == 1.0
        assert result.score >= BAND_MODERATE_THRESHOLD

        # 3. Counterfactual: had the poisoned window applied, the inverted
        # axis would have been zeroed; this is the recall the hold preserves.
        assert (
            normalise_inverted(
                honest_p50,
                p50=poisoned_p50,
                p99=poisoned_p99,
            )
            == 0.0
        )


class TestHeldDriftWarning:
    """The held-drift warning must name the axis/values that actually CAUSED
    the hold, not simply the first axis that drifted (review finding: a
    p50-caused hold logged the p99 axis when p99 had also drifted in an
    applied-safe direction)."""

    def _row(self, feature, p50, p99):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return ("preprod", "per_script", "addrA", feature, p50, p99, 300, now, 90)

    @patch("app.analysis.baselines.clickhouse")
    def test_warning_names_causal_axis_only(self, mock_ch, caplog):
        import logging
        from app.analysis.baselines import _filter_drifted

        mock_ch.get_baseline.return_value = {
            "p50": 5.0,
            "p99": 100.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        # p99 narrows 100 -> 10 (drifted, applied-safe for a pure-normalise
        # feature); p50 rises 5 -> 20 (the hold cause).
        with caplog.at_level(logging.WARNING, logger="app.analysis.baselines"):
            kept = _filter_drifted([self._row("datum_bytes", p50=20.0, p99=10.0)])
        assert kept == []
        held_msgs = [r.getMessage() for r in caplog.records if "HELD" in r.getMessage()]
        assert len(held_msgs) == 1
        assert "p50 5 -> 20" in held_msgs[0]
        assert "p99" not in held_msgs[0]

    @patch("app.analysis.baselines.clickhouse")
    def test_warning_names_both_axes_when_both_cause_hold(self, mock_ch, caplog):
        import logging
        from app.analysis.baselines import _filter_drifted

        mock_ch.get_baseline.return_value = {
            "p50": 5.0,
            "p99": 10.0,
            "sample_count": 300,
            "computed_at": None,
            "window_days": 90,
        }
        with caplog.at_level(logging.WARNING, logger="app.analysis.baselines"):
            kept = _filter_drifted([self._row("datum_bytes", p50=20.0, p99=100.0)])
        assert kept == []
        held_msgs = [r.getMessage() for r in caplog.records if "HELD" in r.getMessage()]
        assert len(held_msgs) == 1
        assert "p99 10 -> 100" in held_msgs[0]
        assert "p50 5 -> 20" in held_msgs[0]


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
