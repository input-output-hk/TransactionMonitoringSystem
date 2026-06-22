"""Guard that the API's ``FeatureSet`` Literal and the feature builder's
``FEATURE_SETS`` tuple stay in sync: they are validated (API) and iterated
(CLI / feature builder) in two places, so a value added to one but not the
other would silently accept-then-fail or skip a set."""

from typing import get_args

from app.api.schemas import FeatureSet
from app.features import FEATURE_SETS


def test_feature_set_literal_matches_feature_sets():
    assert set(get_args(FeatureSet)) == set(FEATURE_SETS)
