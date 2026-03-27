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
        assert normalise(5.0, p50=10.0, p99=10.0) == 0.0
        assert normalise(15.0, p50=10.0, p99=10.0) == 1.0


class TestNormaliseInverted:
    def test_at_p50_returns_one(self):
        assert normalise_inverted(10.0, p50=10.0, p99=20.0) == 1.0

    def test_at_p99_returns_zero(self):
        result = normalise_inverted(20.0, p50=10.0, p99=20.0)
        assert abs(result) < 0.01

    def test_below_p50_clipped_to_one(self):
        assert normalise_inverted(5.0, p50=10.0, p99=20.0) == 1.0


class TestScoreToBand:
    def test_low(self):
        assert score_to_band(0) == "Low"
        assert score_to_band(30) == "Low"

    def test_moderate(self):
        assert score_to_band(31) == "Moderate"
        assert score_to_band(59) == "Moderate"

    def test_high(self):
        assert score_to_band(60) == "High"
        assert score_to_band(79) == "High"

    def test_critical(self):
        assert score_to_band(80) == "Critical"
        assert score_to_band(100) == "Critical"
