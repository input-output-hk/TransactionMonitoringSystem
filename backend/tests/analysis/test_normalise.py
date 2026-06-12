"""Unit tests for the normalisation module."""

from app.analysis.normalise import normalise, normalise_inverted, score_to_band


class TestNormalise:
    def test_at_p50_returns_zero(self):
        assert normalise(10.0, p50=10.0, p99=20.0) == 0.0

    def test_at_p99_returns_one(self):
        result = normalise(20.0, p50=10.0, p99=20.0)
        assert abs(result - 1.0) < 0.01

    def test_below_p50_clipped(self):
        assert normalise(5.0, p50=10.0, p99=20.0) == 0.0

    def test_above_p99_clipped(self):
        assert normalise(100.0, p50=10.0, p99=20.0) == 1.0

    def test_midpoint(self):
        result = normalise(15.0, p50=10.0, p99=20.0)
        assert 0.45 < result < 0.55

    def test_degenerate_p50_equals_p99(self):
        # Zero-variance baselines carry no signal, so both directions return 0.
        assert normalise(5.0, p50=10.0, p99=10.0) == 0.0
        assert normalise(15.0, p50=10.0, p99=10.0) == 0.0


class TestNormaliseInverted:
    def test_at_p50_returns_one(self):
        assert normalise_inverted(10.0, p50=10.0, p99=20.0) == 1.0

    def test_at_p99_returns_zero(self):
        result = normalise_inverted(20.0, p50=10.0, p99=20.0)
        assert abs(result) < 0.01

    def test_below_p50_clipped_to_one(self):
        assert normalise_inverted(5.0, p50=10.0, p99=20.0) == 1.0

    def test_inverted_returns_zero_on_constant_baseline(self):
        # Without this guard, an inverted call against a constant baseline
        # (p50 == p99) flips into 1.0 for every value at or below the constant,
        # which produced the token_dust false-positive cluster on preprod.
        assert normalise_inverted(5.0, p50=10.0, p99=10.0) == 0.0
        assert normalise_inverted(15.0, p50=10.0, p99=10.0) == 0.0


class TestScoreToBand:
    def test_informational(self):
        # 0-30 is the "Informational" band (renamed from "Low" 2026-06).
        assert score_to_band(0) == "Informational"
        assert score_to_band(30) == "Informational"

    def test_moderate(self):
        assert score_to_band(31) == "Moderate"
        assert score_to_band(59) == "Moderate"

    def test_fractional_scores_above_30_are_moderate(self):
        # Scores are floats rounded to 2dp; the (30, 31) interval must not
        # be a dead zone that silently under-bands toward Informational.
        assert score_to_band(30.5) == "Moderate"
        assert score_to_band(30.01) == "Moderate"
        assert score_to_band(30.0) == "Informational"

    def test_high(self):
        assert score_to_band(60) == "High"
        assert score_to_band(79) == "High"

    def test_critical(self):
        assert score_to_band(80) == "Critical"
        assert score_to_band(100) == "Critical"


class TestBaselineSpreadGuard:
    """``resolve_baseline`` must fall through narrow baselines.

    A per-script baseline with near-zero spread between p50 and p99
    collapses ``normalise_inverted`` so that any value at the median
    scores 1.0. Treating those baselines as uninformative and falling
    through to the next tier preserves intended scorer semantics.
    """

    def test_baseline_is_usable_strict_equality(self):
        from app.analysis.normalise import _baseline_is_usable
        assert _baseline_is_usable({"p50": 10.0, "p99": 10.0}) is False

    def test_baseline_is_usable_tight_spread(self):
        from app.analysis.normalise import _baseline_is_usable
        # 2.7% spread — the exact shape seen on the 2026-05-15 state-machine
        # script cluster. Must fall through.
        assert _baseline_is_usable({"p50": 2_519_195.0, "p99": 2_586_000.0}) is False

    def test_baseline_is_usable_healthy_spread(self):
        from app.analysis.normalise import _baseline_is_usable
        # 641% spread — natural variation across UTxOs at a real script.
        assert _baseline_is_usable({"p50": 1_349_030.0, "p99": 10_000_000.0}) is True

    def test_baseline_is_usable_p50_zero(self):
        from app.analysis.normalise import _baseline_is_usable
        # When p50 is zero the ratio is undefined; accept any positive p99
        # (the spread is "infinite" by convention) and reject p99 == 0.
        assert _baseline_is_usable({"p50": 0.0, "p99": 5.0}) is True
        assert _baseline_is_usable({"p50": 0.0, "p99": 0.0}) is False


class TestResolveScopeTypesAllowed:
    """``scope_types_allowed`` restricts which baseline tiers are consulted.

    Used by the multiple_sat extraction axis to resolve per_script -> bootstrap
    and NEVER global: the global distribution of value/assets leaving a script
    is dominated by legitimate high-volume asset-movers, so a global fallback
    would de-sensitise detection on rare/novel scripts.
    """

    @staticmethod
    def _fake_get_baseline(monkeypatch, rows, calls):
        from app.analysis import normalise as norm

        def _fn(network, scope_type, scope_id, feature):
            calls.append((scope_type, feature))
            row = rows.get((scope_type, feature))
            return dict(row) if row else None

        monkeypatch.setattr(norm.clickhouse, "get_baseline", _fn)

    def test_per_script_only_skips_global(self, monkeypatch):
        from app.analysis.normalise import resolve_baseline
        # A usable global baseline exists but per_script does not.
        rows = {("global", "n_assets_out_of_script"):
                {"p50": 1.0, "p99": 5.0, "sample_count": 1000}}
        calls = []
        self._fake_get_baseline(monkeypatch, rows, calls)
        p50, p99, source = resolve_baseline(
            "n_assets_out_of_script", "per_script", "addrX", "preprod",
            scope_types_allowed=["per_script"],
        )
        # global is present but must be ignored -> "missing" (caller bootstraps).
        assert source == "missing"
        assert ("global", "n_assets_out_of_script") not in calls

    def test_default_still_falls_back_to_global(self, monkeypatch):
        from app.analysis.normalise import resolve_baseline
        rows = {("global", "n_assets_out_of_script"):
                {"p50": 1.0, "p99": 5.0, "sample_count": 1000}}
        calls = []
        self._fake_get_baseline(monkeypatch, rows, calls)
        # No scope_types_allowed -> unchanged behaviour: per_script miss falls to global.
        p50, p99, source = resolve_baseline(
            "n_assets_out_of_script", "per_script", "addrX", "preprod",
        )
        assert source == "global"
        assert (p50, p99) == (1.0, 5.0)


class TestRiskBandLegacyAlias:
    """The 0-30 band was renamed "Low" -> "Informational" (2026-06).

    RiskBand must still parse the legacy "Low" so reads of un-migrated rows
    don't raise, regardless of deploy/migration ordering.
    """

    def test_current_label_parses(self):
        from app.models.transaction import RiskBand
        assert RiskBand("Informational") is RiskBand.INFORMATIONAL

    def test_legacy_low_maps_to_informational(self):
        from app.models.transaction import RiskBand
        assert RiskBand("Low") is RiskBand.INFORMATIONAL
        # value is the new canonical label, so API responses serialise consistently
        assert RiskBand("Low").value == "Informational"

    def test_unknown_still_raises(self):
        import pytest
        from app.models.transaction import RiskBand
        with pytest.raises(ValueError):
            RiskBand("Nonexistent")
