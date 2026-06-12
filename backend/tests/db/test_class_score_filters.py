"""Unit tests for the shared class-scores WHERE-clause builder.

Pure tests: ``_score_filter_conditions`` builds (conditions, params) strings and
never touches ClickHouse, so no DB connection is needed.
"""

from app.db.clickhouse import _score_filter_conditions


def _build(**overrides):
    kwargs = dict(
        network="preprod",
        risk_band=None,
        attack_class=None,
        min_score=0.0,
        analyzed_from=None,
        analyzed_to=None,
        include_archived=True,  # skip the archived anti-join for a focused check
    )
    kwargs.update(overrides)
    return _score_filter_conditions(**kwargs)


class TestMinCorroborationFilter:
    def test_filter_added_when_positive(self):
        conditions, params = _build(min_corroboration=2)
        assert "corroboration_count >= %(min_corroboration)s" in conditions
        assert params["min_corroboration"] == 2

    def test_no_filter_when_zero(self):
        conditions, params = _build(min_corroboration=0)
        assert not any("corroboration_count" in c for c in conditions)
        assert "min_corroboration" not in params

    def test_default_is_no_filter(self):
        # Omitting the argument entirely must behave like 0 (no filter), so
        # existing callers that don't pass it keep their current behaviour.
        conditions, params = _build()
        assert not any("corroboration_count" in c for c in conditions)
        assert "min_corroboration" not in params

    def test_parameterized_not_interpolated(self):
        # The value travels as a bound parameter, never string-formatted into
        # the SQL, so it cannot be an injection vector.
        conditions, params = _build(min_corroboration=3)
        clause = next(c for c in conditions if "corroboration_count" in c)
        assert "%(min_corroboration)s" in clause
        assert "3" not in clause
