"""Tests for the recall-marker guard.

A guard that cannot fail is a false control claim, which is worse than no
guard: CI would report the recall guarantee intact whatever the suite actually
contained. So both failure modes are exercised here, along with the live
assertion that the repository currently satisfies the guard.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_recall_markers.py"


def _load():
    """Load the script by path: `scripts/` is not an importable package."""
    spec = importlib.util.spec_from_file_location("check_recall_markers", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load()


def _module(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "test_synthetic.py"
    path.write_text(body, encoding="utf-8")
    return path


class TestMarkedTests:
    def test_band_anchored_marked_test_counts_as_pinned(self, checker, tmp_path):
        pinned, unpinned, inverted = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                "@pytest.mark.attack_must_fire\n"
                "def test_attack(): assert score >= BAND_HIGH_THRESHOLD\n",
            )
        )
        assert pinned == ["test_attack"]
        assert unpinned == []

    def test_marked_test_without_a_band_is_reported(self, checker, tmp_path):
        """The failure mode that matters: the mark spreading to a test that
        asserts a bare literal, so it promises a band it never checks."""
        pinned, unpinned, inverted = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                "@pytest.mark.attack_must_fire\n"
                "def test_attack(): assert score > 20\n",
            )
        )
        assert pinned == []
        assert unpinned == ["test_attack"]

    def test_unmarked_tests_are_ignored(self, checker, tmp_path):
        pinned, unpinned, inverted = checker.marked_tests(
            _module(
                tmp_path,
                "def test_precision(): assert score < BAND_HIGH_THRESHOLD\n"
                "def test_gate(): assert gate is False\n",
            )
        )
        assert (pinned, unpinned, inverted) == ([], [], [])

    def test_a_module_with_no_marked_test_yields_nothing(self, checker, tmp_path):
        """This is what makes a whole attack class fail the guard."""
        pinned, _, _ = checker.marked_tests(
            _module(tmp_path, "def test_attack(): assert score >= BAND_HIGH_THRESHOLD\n")
        )
        assert pinned == []

    def test_marker_is_found_beside_other_decorators(self, checker, tmp_path):
        pinned, _, _ = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                '@pytest.mark.parametrize("n", [1, 2])\n'
                "@pytest.mark.attack_must_fire\n"
                "def test_attack(n): assert score >= BAND_CRITICAL_THRESHOLD\n",
            )
        )
        assert pinned == ["test_attack"]

    def test_marker_is_found_on_a_method_inside_a_class(self, checker, tmp_path):
        pinned, _, _ = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                "class TestScore:\n"
                "    @pytest.mark.attack_must_fire\n"
                "    def test_attack(self): assert score >= BAND_HIGH_THRESHOLD\n",
            )
        )
        assert pinned == ["test_attack"]

    def test_score_bounded_from_above_is_reported_as_inverted(self, checker, tmp_path):
        """A precision case wearing a recall mark. It can satisfy the pinning
        regex through an incidental comparison, so it is rejected on the upper
        bound rather than accepted on the lower one."""
        pinned, unpinned, inverted = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                "@pytest.mark.attack_must_fire\n"
                "def test_capped():\n"
                "    assert result.score <= BAND_MODERATE_MAX\n"
                "    assert uncapped(result) >= BAND_CRITICAL_THRESHOLD\n",
            )
        )
        assert (pinned, unpinned, inverted) == ([], [], ["test_capped"])

    def test_a_weight_bounded_from_above_is_still_pinned(self, checker, tmp_path):
        """The premise of a band-floor case: proving the shape cannot reach the
        band on weights alone is an upper bound on a WEIGHT, not on the score,
        and must not disqualify the mark."""
        pinned, unpinned, inverted = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                "@pytest.mark.attack_must_fire\n"
                "def test_floored():\n"
                "    assert (_W_A + _W_B) * 100 < BAND_HIGH_THRESHOLD\n"
                "    assert result.score >= BAND_HIGH_THRESHOLD\n",
            )
        )
        assert (pinned, unpinned, inverted) == (["test_floored"], [], [])

    def test_an_async_marked_test_is_seen(self, checker, tmp_path):
        """Otherwise a marked async test would be invisible to the guard while
        still running under `pytest -m`."""
        pinned, _, _ = checker.marked_tests(
            _module(
                tmp_path,
                "import pytest\n"
                "@pytest.mark.attack_must_fire\n"
                "async def test_attack(): assert result.score >= BAND_HIGH_THRESHOLD\n",
            )
        )
        assert pinned == ["test_attack"]


class TestAttackClasses:
    def test_excludes_the_read_time_only_class(self, checker):
        """`contract_anomaly` is the sidecar's verdict, not a scorer, so it has
        no scorer test module to demand a marked case from."""
        classes = checker.attack_classes(checker.repo_root())
        assert "contract_anomaly" not in classes
        assert "multiple_sat" in classes

    def test_stale_exclusion_is_an_error_not_a_silent_pass(self, checker, monkeypatch):
        """If a name in the exclusion list stops being an AttackClass, the
        exclusion silently stops excluding anything. Fail loudly instead."""
        monkeypatch.setattr(checker, "NOT_SCORER_CLASSES", frozenset({"renamed_away"}))
        with pytest.raises(SystemExit, match="not in AttackClass"):
            checker.attack_classes(checker.repo_root())


class TestThisRepository:
    def test_the_guarantee_is_currently_intact(self, checker):
        """The guard in the suite as well as in CI, so a local run catches a
        removed must-fire case without waiting for a push."""
        assert checker.main() == 0
